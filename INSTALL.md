# Installation Instructions

1. Install general python packages
```bash
conda create -n midi python==3.10

conda activate midi
```

2. manually install cuda and torch. To allow local compiled RLbench co-exist with torch, we have to install torch from pip, to avoid MKL, gcc, ninja issues ...

```
conda install -c nvidia/label/cuda-12.1.0 cuda

pip install torch==2.3.0 torchvision==0.18.0 torchaudio==2.3.0 --index-url https://download.pytorch.org/whl/cu121
```

(Optional, but recommend) If you are using GPUs higher than V100, compile and install flash-attn

```
export CUDA_HOME=$HOME/conda/envs/midi
export CPATH=$CUDA_HOME/targets/x86_64-linux/include:$CPATH
export LD_LIBRARY_PATH=$CUDA_HOME/targets/x86_64-linux/lib:$LD_LIBRARY_PATH
export PATH=$CUDA_HOME/bin:$PATH

pip install flash_attn==2.5.9.post1

```

3. Install wheels
```
pip install torch_scatter==2.1.2 --find-links https://pytorch-geometric.com/whl/torch-2.3.0%2Bcu121.html
```
4. Install other requirements
```
pip install -r requirements.txt
```

# Install This Package
```
pip install -e .
```

# For headless RLBench evaluation
x11 related lib
```
sudo apt-get install xorg libxcb-randr0-dev libxrender-dev libxkbcommon-dev libxkbcommon-x11-0 libavcodec-dev libavformat-dev libswscale-dev apt-get install -y --no-install-recommends libgl1-mesa-dev xvfb dbus-x11 x11-utils libxkbcommon-x11-0
```

```bash
mkdir dependencies
cd dependencies
```

Download CoppeliaSim (see instructions [here](https://github.com/stepjam/PyRep?tab=readme-ov-file#install))
```bash
# change the version if necessary
wget https://www.coppeliarobotics.com/files/V4_1_0/CoppeliaSim_Edu_V4_1_0_Ubuntu20_04.tar.xz
tar -xvf CoppeliaSim_Edu_V4_1_0_Ubuntu20_04.tar.xz
```

Add the following to your ~/.bashrc file:
```bash
export COPPELIASIM_ROOT=$(pwd)/CoppeliaSim_Edu_V4_1_0_Ubuntu20_04
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$COPPELIASIM_ROOT
export QT_QPA_PLATFORM_PLUGIN_PATH=$COPPELIASIM_ROOT
```

Install Pyrep and RLBench
```bash
git clone https://github.com/cshizhe/PyRep.git
cd PyRep
pip install -r requirements.txt
pip install .
cd ..

# GemBench modified version of RLBench, fork to fix version.
git clone https://github.com/utomm/RLBench.git
cd RLBench
pip install -r requirements.txt
pip install .
cd ../..
```

3. Install model dependencies

```bash
cd dependencies

# Please ensure to set CUDA_HOME beforehand as specified in the export const of the section 1
git clone https://github.com/cshizhe/chamferdist.git
cd chamferdist
python setup.py install
cd ..

git clone https://github.com/cshizhe/Pointnet2_PyTorch.git
cd Pointnet2_PyTorch/pointnet2_ops_lib
python setup.py install
cd ../..

# llama3: needed for 3D-LOTUS++
git clone https://github.com/cshizhe/llama3.git
cd llama3
pip install -e .
cd ../..
```

4. Running headless

If you have sudo priviledge on the headless machine, you could follow [this instruction](https://github.com/rjgpinel/RLBench?tab=readme-ov-file#running-headless) to run RLBench.

Otherwise, you can use [singularity](https://apptainer.org/docs/user/1.3/index.html) or [docker](https://docs.docker.com/) to run RLBench in headless machines without sudo privilege.
The [XVFB](https://manpages.ubuntu.com/manpages/xenial/man1/xvfb-run.1.html) should be installed in the virtual image in order to have a virtual screen.

A pre-built singularity image can be downloaded [here](https://www.dropbox.com/scl/fi/wnf27yd4pkeywjk2y3wd4/nvcuda_v2.sif?rlkey=7lpni7d9b6dwjj4wehldq8037&st=5steya0b&dl=0).
Here are some simple commands to run singularity:
```bash
export SINGULARITY_IMAGE_PATH=`YOUR PATH TO THE SINGULARITY IMAGE`
export python_bin=$HOME/miniconda3/envs/gembench/bin/python

# interactive mode
singularity shell --bind $HOME:$HOME,$SCRATCH:$SCRATCH --nv $SINGULARITY_IMAGE_PATH

# run script
singularity exec --bind $HOME:$HOME,$SCRATCH:$SCRATCH --nv $SINGULARITY_IMAGE_PATH xvfb-run -a ${python_bin} ...
```

5. Adapt the codebase to your environment

To adapt the codebase to your environment, you may need to modify the following:
- replace everywhere $HOME/codes/robot-3dlotus with your path to robot-3dlotus folder
- replace everywhere the sif_image path to your path to the singularity image nvcuda_v2.sif
