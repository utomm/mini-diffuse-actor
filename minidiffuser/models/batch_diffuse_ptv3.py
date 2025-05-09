from scipy.spatial.transform import Rotation as R

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import einops

from diffusers.schedulers.scheduling_ddpm import DDPMScheduler

from minidiffuser.utils.rotation_transform import discrete_euler_to_quaternion
from minidiffuser.models.base import BaseModel, RobotPoseEmbedding
from minidiffuser.utils.rotation_transform import RotationMatrixTransform
from minidiffuser.models.PointTransformerV3.model import (
    PointTransformerV3, offset2bincount, offset2batch
)
from minidiffuser.models.PointTransformerV3.model_with_neck import PTv3withNeck
from minidiffuser.utils.action_position_utils import get_best_pos_from_disc_pos


class SinusoidalTimestepEmbedding(torch.nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim
        self.LOG_10000 = -9.2103

    def forward(self, timesteps: torch.Tensor):
        half_dim = self.dim // 2
        freqs = torch.exp(self.LOG_10000 * torch.arange(half_dim, dtype=torch.float32, device=timesteps.device) / half_dim)
        args = timesteps[:, None] * freqs[None, :]
        return torch.cat([torch.sin(args), torch.cos(args)], dim=-1)



class ActionHead(nn.Module):
    def __init__(
        self, reduce, pos_pred_type, rot_pred_type, hidden_size, dim_actions, 
        dropout=0, voxel_size=0.01, euler_resolution=5, ptv3_config=None, pos_bins=50,
    ) -> None:
        super().__init__()
        assert reduce in ['max', 'mean', 'attn', 'multiscale_max', 'multiscale_max_large']
        assert pos_pred_type in ['heatmap_mlp', 'heatmap_mlp3', 'heatmap_mlp_topk', 'heatmap_mlp_clf', 'heatmap_normmax', 'heatmap_disc', 'diffuse']
        assert rot_pred_type in ['quat', 'rot6d', 'euler', 'euler_delta', 'euler_disc']

        self.reduce = reduce
        self.pos_pred_type = pos_pred_type
        self.rot_pred_type = rot_pred_type
        self.hidden_size = hidden_size
        self.dim_actions = dim_actions
        self.voxel_size = voxel_size
        self.euler_resolution = euler_resolution
        self.euler_bins = 360 // euler_resolution
        self.pos_bins = pos_bins

        if self.pos_pred_type == 'heatmap_disc':
            self.heatmap_mlp = nn.Sequential(
                nn.Linear(hidden_size, hidden_size),
                nn.LeakyReLU(0.02),
                nn.Dropout(dropout),
                nn.Linear(hidden_size, 3 * self.pos_bins * 2)
            )
        else:
            output_size = 1 + 3
            self.heatmap_mlp = nn.Sequential(
                nn.Linear(hidden_size, hidden_size),
                nn.LeakyReLU(0.02),
                nn.Dropout(dropout),
                nn.Linear(hidden_size, output_size)
            )

        if self.rot_pred_type == 'euler_disc':
            output_size = self.euler_bins * 3 + 1
        else:
            output_size = dim_actions - 3
        if self.reduce == 'attn':
            output_size += 1
        input_size = hidden_size
        
        self.action_mlp = nn.Sequential(
            nn.Linear(input_size + 3, hidden_size),
            nn.LeakyReLU(0.02),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, output_size)
        )

        self.noise_t_embedding = SinusoidalTimestepEmbedding(10)

        self.denoise_mlp = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.LeakyReLU(0.02),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, 3),
        )
        
    def conditioned_rot(
        self, point_embeds, npoints_in_batch, pos_condition
    ):
        '''
        Args:
            point_embeds: (# all points, dim)
            npoints_in_batch: (batch_size, )
            pos_condition: (batch_size, 3)
        Return:
            pred_actions: (batch, num_steps, dim_actions)
        ''' 



        if self.reduce == 'max':
            split_point_embeds = torch.split(point_embeds, npoints_in_batch)
            pc_embeds = torch.stack([torch.max(x, 0)[0] for x in split_point_embeds], 0)
            # print("pc_embeds_shape", pc_embeds.shape)
            # print("pos_condition_shape", pos_condition.shape)
            pc_embeds = torch.cat([pc_embeds, pos_condition], dim=-1)
            action_embeds = self.action_mlp(pc_embeds)
            
        if self.rot_pred_type == 'quat':
            xr = action_embeds[..., :4]
            xr = xr / xr.square().sum(dim=-1, keepdim=True).sqrt()
        elif self.rot_pred_type == 'rot6d':
            xr = action_embeds[..., :6]
        elif self.rot_pred_type in ['euler', 'euler_delta']:
            xr = action_embeds[..., :3]
        elif self.rot_pred_type == 'euler_disc':
            xr = action_embeds[..., :self.euler_bins*3].view(-1, self.euler_bins, 3)
        else:
            raise NotImplementedError

        
        xo = action_embeds[..., -1]
        

        return xr, xo

    def forward_diffuse(self, point_embeds):
        # slpit and stack at the first dim
        # print("emb shape", point_embeds.shape)
        # split_point_embeds = torch.split(point_embeds, num_mini_batches)
        # batched_point_embeds = torch.stack(split_point_embeds, 0)
        return self.denoise_mlp(point_embeds)
        


class DiffPolicyPTV3(BaseModel):
    """Cross attention conditioned on text/pose/stepid
    """
    def __init__(self, config):
        BaseModel.__init__(self)

        self.config = config

        self.ptv3_model = PTv3withNeck(**config.ptv3_config)

        # print model trainable parameters total number
        total_params = sum(p.numel() for p in self.ptv3_model.parameters() if p.requires_grad)
        print(f"Total trainable parameters in PTV3 backbone: {total_params}")


        act_cfg = config.action_config
        self.txt_fc = nn.Linear(act_cfg.txt_ft_size, act_cfg.context_channels)


        self.txt_fc = nn.Linear(act_cfg.txt_ft_size, act_cfg.context_channels)
        if act_cfg.txt_reduce == 'attn':
            self.txt_attn_fc = nn.Linear(act_cfg.txt_ft_size, 1)
        if act_cfg.use_ee_pose:
            self.pose_embedding = RobotPoseEmbedding(act_cfg.context_channels)
        if act_cfg.use_step_id:
            self.stepid_embedding = nn.Embedding(act_cfg.max_steps, act_cfg.context_channels)
        self.act_proj_head = ActionHead(
            act_cfg.reduce, act_cfg.pos_pred_type, act_cfg.rot_pred_type, 
            config.ptv3_config.dec_channels[0], act_cfg.dim_actions, 
            dropout=act_cfg.dropout, voxel_size=act_cfg.voxel_size,
            ptv3_config=config.ptv3_config, pos_bins=config.action_config.pos_bins,
        )

        # print action head trainable parameters total number
        total_params = sum(p.numel() for p in self.act_proj_head.parameters() if p.requires_grad)
        print(f"Total trainable parameters in Action Head: {total_params}")

        self.apply(self._init_weights)

        self.rot_transform = RotationMatrixTransform()

        self.position_noise_scheduler = DDPMScheduler(
            num_train_timesteps=self.config.diffusion.total_timesteps,
            beta_schedule="scaled_linear",
            prediction_type="epsilon",
        )

        # simply a 3 dim tensor represent the anchor identity
        self.anchor_embeding = nn.Parameter(torch.zeros(1, 3))
        self.noise_embedding = SinusoidalTimestepEmbedding(256)

    def prepare_ptv3_batch(self, batch):
        outs = {
            'coord': batch['pc_fts'][:, :3],
            'grid_size': self.config.action_config.voxel_size,
            'offset': batch['offset'],
            'batch': offset2batch(batch['offset']),
            'feat': batch['pc_fts'],
        }
        device = batch['pc_fts'].device
        # print("samples on device:", device)

        # encode context for each point cloud
        txt_embeds = self.txt_fc(batch['txt_embeds'])
        ctx_embeds = torch.split(txt_embeds, batch['txt_lens'])
        ctx_lens = torch.LongTensor(batch['txt_lens'])

        if self.config.action_config.use_ee_pose:
            pose_embeds = self.pose_embedding(batch['ee_poses'])
            ctx_embeds = [torch.cat([c, e.unsqueeze(0)], dim=0) for c, e in zip(ctx_embeds, pose_embeds)]
            ctx_lens += 1

        if self.config.action_config.use_step_id:
            step_embeds = self.stepid_embedding(batch['step_ids'])
            ctx_embeds = [torch.cat([c, e.unsqueeze(0)], dim=0) for c, e in zip(ctx_embeds, step_embeds)]
            ctx_lens += 1

        outs['context'] = torch.cat(ctx_embeds, 0)
        outs['context_offset'] = torch.cumsum(ctx_lens, dim=0).to(device)

        return outs

    
    def prepare_noise_anchor(self, batch):
        gt_trans = batch['gt_actions'][:, :3]
        # repeat n times as mini-batch size
        num_repeat = self.config.mini_batches
        gt_trans = einops.repeat(gt_trans, 'n c -> (n k) c', k=num_repeat)
        noise = torch.randn(gt_trans.shape, device=gt_trans.device)
        noise_steps = torch.randint(
            0,
            self.position_noise_scheduler.config.num_train_timesteps,
            (noise.shape[0],),
            device=noise.device,
        ) # .long()?

        trans_input = self.position_noise_scheduler.add_noise(
            gt_trans, noise, noise_steps
        )

        # encode context for each point cloud
        ctx_embeds = self.txt_fc(batch['txt_embeds'])
        if self.config.action_config.txt_reduce == 'attn':
            txt_weights = torch.split(self.txt_attn_fc(batch['txt_embeds']), batch['txt_lens'])
            txt_embeds = torch.split(ctx_embeds, batch['txt_lens'])
            ctx_embeds = []
            for txt_weight, txt_embed in zip(txt_weights, txt_embeds):
                txt_weight = torch.softmax(txt_weight, 0)
                ctx_embeds.append(torch.sum(txt_weight * txt_embed, 0))
            ctx_embeds = torch.stack(ctx_embeds, 0)
        
        if self.config.action_config.use_ee_pose:
            pose_embeds = self.pose_embedding(batch['ee_poses'])
            ctx_embeds += pose_embeds

        if self.config.action_config.use_step_id:
            step_embeds = self.stepid_embedding(batch['step_ids'])
            ctx_embeds += step_embeds


        context_emb = self.noise_embedding(noise_steps)


        ctx_embeds = einops.repeat(ctx_embeds, 'i j -> (repeat i) j', repeat=num_repeat)
        context_emb = context_emb + ctx_embeds
        batch["trans_input"] = trans_input
        batch["gt_noise"] = noise
        batch["noise_steps"] = noise_steps

        npoints_in_batch = torch.ones(batch['gt_actions'].shape[0], dtype=torch.long) * num_repeat
        # offset is [n1, n1+n2, n1+n2+n3, ...]
        offset = torch.cumsum(torch.LongTensor(npoints_in_batch), dim=0).to(trans_input.device)

        # print("trans_input_shape", trans_input.shape)
        # print("context_emb_shape", context_emb.shape)

        outs = {
            'coord': trans_input,
            'grid_size': self.config.action_config.voxel_size,
            'offset': offset,
            'batch': offset2batch(offset), # tensor([0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2])
            'feat': trans_input.clone(), 
            'context': context_emb,
        }
        return outs


    def forward(self, batch: dict, compute_loss=False, **kwargs):
        '''batch data:
            pc_fts: (batch, npoints, dim)
            txt_embeds: (batch, txt_dim)
            # batch already concate by dataloader fetch func
        '''

        # print("========== batch info ==========")
        # print(len(batch['npoints_in_batch']))
        # print(batch['npoints_in_batch'])
        # print("========== batch info ==========")
        
        # if torch.eval, then turn to forward_n_steps
        if not self.training:
            return self.forward_n_steps(batch, compute_loss, **kwargs)

        batch = self.prepare_batch(batch) # TODO: change here to add noise
        
        
        # batch = self.add_diffusion_noise(batch)



        # print("points shape", batch['pc_fts'].shape)

        device = batch['pc_fts'].device

        ptv3_batch = self.prepare_ptv3_batch(batch)

        anchor = self.prepare_noise_anchor(batch)
        
        
        # add gussian noise 0.01
        pos_condition = batch['gt_actions'][..., :3] + torch.randn(batch['gt_actions'][..., :3].shape, device=batch['gt_actions'].device) * 0.01
        # in training, use noise gt_pose

        point_outs, anchor_outs = self.ptv3_model.forward_train(ptv3_batch, anchor, return_dec_layers=True)

        # anchor_outs = self.ptv3_model.forward_only_neck(anchor)

        # print("forward_success!", anchor_outs)

        # predict posi from noise
        # predict rot and openness

        pred_pos = self.act_proj_head.forward_diffuse(
            anchor_outs.feat
        )
        
        pred_rot, pred_open = self.act_proj_head.conditioned_rot(
            point_outs[-1].feat, batch['npoints_in_batch'], pos_condition=pos_condition
        )
          

        action = pred_pos, pred_rot, pred_open

        if self.config.action_config.rot_pred_type == 'rot6d':
            # no grad
            pred_rot = self.rot_transform.matrix_to_quaternion(
                self.rot_transform.compute_rotation_matrix_from_ortho6d(pred_rot.data.cpu())
            ).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler':
            pred_rot = pred_rot * 180
            pred_rot = self.rot_transform.euler_to_quaternion(pred_rot.data.cpu()).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler_delta':
            pred_rot = pred_rot * 180
            cur_euler_angles = R.from_quat(batch['ee_poses'][..., 3:7].data.cpu()).as_euler('xyz', degrees=True)
            pred_rot = pred_rot.data.cpu() + cur_euler_angles
            pred_rot = self.rot_transform.euler_to_quaternion(pred_rot).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler_disc':
            pred_rot = torch.argmax(pred_rot, 1).data.cpu().numpy()
            pred_rot = np.stack([discrete_euler_to_quaternion(x, self.act_proj_head.euler_resolution) for x in pred_rot], 0)
            pred_rot = torch.from_numpy(pred_rot).to(device)
        
        # final_pred_actions = torch.cat([pred_pos, pred_rot, pred_open.unsqueeze(-1)], dim=-1)

        final_pred_actions = None
        
        if compute_loss:
            losses = self.compute_loss(
                action, batch['gt_actions'], 
                pos_noise=batch['gt_noise'], npoints_in_batch=batch['npoints_in_batch'])
            return final_pred_actions, losses
        else:
            return final_pred_actions

    @torch.no_grad()
    def forward_n_steps(self, batch: dict, compute_loss=False, is_dataset=False, **kwargs):
        '''batch data:
            pc_fts: (batch, npoints, dim)
            txt_embeds: (batch, txt_dim)
            # batch already concate by dataloader fetch func
        '''
        batch = self.prepare_batch(batch) # TODO: change here to add noise

        self.position_noise_scheduler.set_timesteps(
            self.config.diffusion.total_timesteps
        )

        
        
        gt_trans = batch['ee_poses'][:, :3] # borrow the shape
        init_anchor = torch.zeros_like(gt_trans)
        noise = torch.randn(gt_trans.shape, device=gt_trans.device)
        noise_steps = torch.ones(
            (noise.shape[0], ),
            device=noise.device,
        ).mul(self.position_noise_scheduler.timesteps[0]).long()

        trans_input = self.position_noise_scheduler.add_noise(
            init_anchor, noise, noise_steps
        )

        batch["trans_input"] = trans_input
        batch["gt_noise"] = noise
        batch["noise_steps"] = noise_steps

        # print("points shape", batch['pc_fts'].shape)

        device = batch['pc_fts'].device

        ptv3_batch = self.prepare_ptv3_batch(batch)
        
        if is_dataset:
            gt_pos = batch['gt_actions'][:, :3]

        point_outs = self.ptv3_model.forward_inference(ptv3_batch, return_dec_layers=True)

        # predict posi from noise
        # predict rot and openness 

        timesteps = self.position_noise_scheduler.timesteps # [inverse order]


        # encode context for each point cloud
        ctx_embeds = self.txt_fc(batch['txt_embeds'])
        if self.config.action_config.txt_reduce == 'attn':
            txt_weights = torch.split(self.txt_attn_fc(batch['txt_embeds']), batch['txt_lens'])
            txt_embeds = torch.split(ctx_embeds, batch['txt_lens'])
            ctx_embeds = []
            for txt_weight, txt_embed in zip(txt_weights, txt_embeds):
                txt_weight = torch.softmax(txt_weight, 0)
                ctx_embeds.append(torch.sum(txt_weight * txt_embed, 0))
            ctx_embeds = torch.stack(ctx_embeds, 0)
        
        if self.config.action_config.use_ee_pose:
            pose_embeds = self.pose_embedding(batch['ee_poses'])
            ctx_embeds += pose_embeds

        if self.config.action_config.use_step_id:
            step_embeds = self.stepid_embedding(batch['step_ids'])
            ctx_embeds += step_embeds

        batch["trans_input"] = trans_input
        batch["gt_noise"] = noise
        batch["noise_steps"] = noise_steps

        npoints_in_batch = torch.ones(batch['ee_poses'].shape[0], dtype=torch.long)
        # offset is [n1, n1+n2, n1+n2+n3, ...]
        offset = torch.cumsum(torch.LongTensor(npoints_in_batch), dim=0).to(trans_input.device)

        # print("trans_input_shape", trans_input.shape)
        # print("context_emb_shape", context_emb.shape)

        outs = {
            'coord': trans_input,
            'grid_size': self.config.action_config.voxel_size,
            'offset': offset,
            'batch': offset2batch(offset), # tensor([0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2])
            'feat': trans_input, 
            'context': None, # to be update in the loop
        }

        pred_pos, pred_rot, pred_open = None, None, None

        for t in timesteps:
            noise_steps = t * torch.ones(len(init_anchor), device = init_anchor.device).long()

            context_emb = self.noise_embedding(noise_steps)
            outs['context'] = ctx_embeds + context_emb
            anchor_outs = self.ptv3_model.neck_inference(outs)
            pred_noise = self.act_proj_head.forward_diffuse(
                anchor_outs.feat
            )
            

            # update coord and context

            outs['coord'] = self.position_noise_scheduler.step(pred_noise, t, outs['coord']).prev_sample
            outs['feat'] = outs['coord'].clone()
            

        pred_pos = outs['coord']
        
        pred_rot, pred_open = self.act_proj_head.conditioned_rot(
            point_outs[-1].feat, batch['npoints_in_batch'], pos_condition=pred_pos
        )

        if self.config.action_config.rot_pred_type == 'rot6d':
            # no grad
            pred_rot = self.rot_transform.matrix_to_quaternion(
                self.rot_transform.compute_rotation_matrix_from_ortho6d(pred_rot.data.cpu())
            ).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler':
            pred_rot = pred_rot * 180
            pred_rot = self.rot_transform.euler_to_quaternion(pred_rot.data.cpu()).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler_delta':
            pred_rot = pred_rot * 180
            cur_euler_angles = R.from_quat(batch['ee_poses'][..., 3:7].data.cpu()).as_euler('xyz', degrees=True)
            pred_rot = pred_rot.data.cpu() + cur_euler_angles
            pred_rot = self.rot_transform.euler_to_quaternion(pred_rot).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler_disc':
            pred_rot = torch.argmax(pred_rot, 1).data.cpu().numpy()
            pred_rot = np.stack([discrete_euler_to_quaternion(x, self.act_proj_head.euler_resolution) for x in pred_rot], 0)
            pred_rot = torch.from_numpy(pred_rot).to(device)
            
        if is_dataset:
            # pred rot from gt pos
            rot_from_gt, open_from_gt = self.act_proj_head.conditioned_rot(
            point_outs[-1].feat, batch['npoints_in_batch'], pos_condition=gt_pos
        )
            
            if self.config.action_config.rot_pred_type == 'rot6d':
                # no grad
                rot_from_gt = self.rot_transform.matrix_to_quaternion(
                    self.rot_transform.compute_rotation_matrix_from_ortho6d(rot_from_gt.data.cpu())
                ).float().to(device)
            elif self.config.action_config.rot_pred_type == 'euler':
                rot_from_gt = rot_from_gt * 180
                rot_from_gt = self.rot_transform.euler_to_quaternion(rot_from_gt.data.cpu()).float().to(device)
            elif self.config.action_config.rot_pred_type == 'euler_delta':
                rot_from_gt = rot_from_gt * 180
                cur_euler_angles = R.from_quat(batch['ee_poses'][..., 3:7].data.cpu()).as_euler('xyz', degrees=True)
                rot_from_gt = rot_from_gt.data.cpu() + cur_euler_angles
                rot_from_gt = self.rot_transform.euler_to_quaternion(rot_from_gt).float().to(device)
            elif self.config.action_config.rot_pred_type == 'euler_disc':
                rot_from_gt = torch.argmax(rot_from_gt, 1).data.cpu().numpy()
                rot_from_gt = np.stack([discrete_euler_to_quaternion(x, self.act_proj_head.euler_resolution) for x in rot_from_gt], 0)
                rot_from_gt = torch.from_numpy(rot_from_gt).to(device)
        
        
        
        self.ptv3_model.clear_cache()
        
        if is_dataset:
            final_pred_actions = torch.cat([pred_pos, pred_rot, pred_open.unsqueeze(-1), rot_from_gt, open_from_gt.unsqueeze(-1)], dim=-1)
            
        else:
            final_pred_actions = torch.cat([pred_pos, pred_rot, pred_open.unsqueeze(-1)], dim=-1)
        

        return final_pred_actions

    def compute_loss(self, pred_actions, tgt_actions, pos_noise, npoints_in_batch=None):
        """
        Args:
            pred_actions: (batch_size, max_action_len, dim_action)
            tgt_actions: (all_valid_actions, dim_action) / (batch_size, max_action_len, dim_action)
            masks: (batch_size, max_action_len)
        """
        # loss_cfg = self.config.loss_config
        device = tgt_actions.device

        pred_pos_noise, pred_rot, pred_open = pred_actions
        tgt_rot, tgt_open = tgt_actions[..., 3:-1], tgt_actions[..., -1]

        # print("pred_pos_noise's shape", pred_pos_noise.shape)
        # print("pos_noise's shape", pos_noise.shape)

        # position loss
        if self.config.loss_config.get('pos_metric', 'l2') == 'l2':
            trans_loss = F.mse_loss(
            pred_pos_noise, pos_noise, reduction="mean"
            )
        else:
            # print("using l1 loss")
            trans_loss = F.l1_loss(
            pred_pos_noise, pos_noise, reduction="mean"
            )

        # if torch.isnan(pred_pos_noise).any() or torch.isnan(pos_noise).any():
        #     print("there is nan in pred_pos_noise or pos_noise")
        # if torch.isinf(pred_pos_noise).any() or torch.isinf(pos_noise).any():
        #     print("there is inf in pred_pos_noise or pos_noise")

        # print("pred_pos_noise")
        # print(pred_pos_noise)
        # print("pos_noise")
        # print(pos_noise)

        # print(trans_loss)

        # rotation loss
        if self.config.action_config.rot_pred_type == 'quat':
            # Automatically matching the closest quaternions (symmetrical solution)
            tgt_rot_ = -tgt_rot.clone()
            rot_loss = F.mse_loss(pred_rot, tgt_rot, reduction='none').mean(-1)
            rot_loss_ = F.mse_loss(pred_rot, tgt_rot_, reduction='none').mean(-1)
            select_mask = (rot_loss < rot_loss_).float()
            rot_loss = (select_mask * rot_loss + (1 - select_mask) * rot_loss_).mean()
        elif self.config.action_config.rot_pred_type == 'rot6d':
            tgt_rot6d = self.rot_transform.get_ortho6d_from_rotation_matrix(
                self.rot_transform.quaternion_to_matrix(tgt_rot.data.cpu())
            ).float().to(device)
            rot_loss = F.mse_loss(pred_rot, tgt_rot6d)
        elif self.config.action_config.rot_pred_type == 'euler':
            # Automatically matching the closest angles
            tgt_rot_ = tgt_rot.clone()
            tgt_rot_[tgt_rot < 0] += 2
            tgt_rot_[tgt_rot > 0] -= 2
            rot_loss = F.mse_loss(pred_rot, tgt_rot, reduction='none')
            rot_loss_ = F.mse_loss(pred_rot, tgt_rot_, reduction='none')
            select_mask = (rot_loss < rot_loss_).float()
            rot_loss = (select_mask * rot_loss + (1 - select_mask) * rot_loss_).mean()
        elif self.config.action_config.rot_pred_type == 'euler_disc':
            tgt_rot = tgt_rot.long()    # (batch_size, 3)
            rot_loss = F.cross_entropy(pred_rot, tgt_rot, reduction='mean')
        else: # euler_delta
            rot_loss = F.mse_loss(pred_rot, tgt_rot)
            
        # openness state loss
        open_loss = F.binary_cross_entropy_with_logits(pred_open, tgt_open, reduction='mean')

        total_loss = self.config.loss_config.pos_weight * trans_loss + \
                     self.config.loss_config.rot_weight * rot_loss + open_loss

        # total_loss = self.config.loss_config.rot_weight * rot_loss + open_loss
        
        return {
            'pos': trans_loss, 'rot': rot_loss, 'open': open_loss, 
            'total': total_loss
        }

