import argparse
import wandb
import copy
from tqdm import tqdm
from statistics import mean, stdev
from sklearn import metrics
from collections import defaultdict
import pickle
import numpy as np
from PIL import Image

import torch
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from torch.utils.data import Dataset

from inverse_stable_diffusion import InversableStableDiffusionPipeline
from diffusers import DPMSolverMultistepScheduler
from optim_utils import *
from io_utils import *

# import os
# os.environ["CUDA_VISIBLE_DEVICES"]="1,2,3"

class OptimizedDataset(Dataset):
    def __init__(
        self,
        data_root,
        size=512,
        repeats=10,
        interpolation="bicubic",
        set="train",
        center_crop=False,
    ):

        self.data_root = data_root
        self.size = size
        self.center_crop = center_crop

        file_list = os.listdir(self.data_root)
        file_list.sort(key=lambda x: int(x.split('-')[-1].split('.')[0]))  # ori-lg7.5-xx.jpg
        self.image_paths = [os.path.join(self.data_root, file_path) for file_path in file_list]
        self.dataset, self.prompt_key = get_dataset(args)
        
        self.num_images = len(self.image_paths)
        self._length = self.num_images

        if set == "train":
            self._length = self.num_images * repeats

        self.interpolation = {
            "bilinear": Image.BILINEAR,
            "bicubic": Image.BICUBIC,
            "lanczos": Image.LANCZOS,
        }[interpolation]

    def __len__(self):
        return self._length

    def __getitem__(self, i):
        example = {}
        image = Image.open(self.image_paths[i % self.num_images])

        if not image.mode == "RGB":
            image = image.convert("RGB")

        text = self.dataset[i % self.num_images][self.prompt_key]
        example["prompt"] = text

        # default to score-sde preprocessing
        img = np.array(image).astype(np.uint8)

        if self.center_crop:
            crop = min(img.shape[0], img.shape[1])
            h, w, = (
                img.shape[0],
                img.shape[1],
            )
            img = img[(h - crop) // 2 : (h + crop) // 2, (w - crop) // 2 : (w + crop) // 2]

        image = Image.fromarray(img)
        image = image.resize((self.size, self.size), resample=self.interpolation)

        example["pixel_values"] = get_img_tensor(image) 
        
        return example

logger = get_logger(__name__)

hyperparameters = {
    "learning_rate": 5e-04,
    "scale_lr": True,
    "max_train_steps": 2000, 
    "save_steps": 500,
    "train_batch_size": 1,
    "gradient_accumulation_steps": 1,
    "gradient_checkpointing": True,
    "mixed_precision": "fp16",
    "seed": 42,
    "output_dir": "sd-concept-output"
}

def create_dataloader(train_dataset, train_batch_size=1):
    return torch.utils.data.DataLoader(train_dataset, batch_size=train_batch_size, shuffle=True)

def main(args):
    # load diffusion model
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    print(f'device: {device}')
    
    save_path = 'ckpts'

    scheduler = DPMSolverMultistepScheduler.from_pretrained(args.model_id, subfolder='scheduler')
    pipe = InversableStableDiffusionPipeline.from_pretrained(
        args.model_id,
        scheduler=scheduler,
        torch_dtype=torch.float16,
        revision='fp16',
        )
    pipe = pipe.to(device)

    train_dataset = OptimizedDataset(
      data_root=args.data_root,
      repeats=10,
      center_crop=False,
      set="train",
)

    train_dataloader = create_dataloader(train_dataset, hyperparameters['train_batch_size'])
    init_latents_w = pipe.get_random_latents()
    opt_watermark = get_watermarking_pattern(pipe, args, device)  # random generate an initial watermark
    mask = get_watermarking_mask(init_latents_w, args, device).detach().cpu()
    pipe.optimizer_wm_prompt(train_dataloader,hyperparameters, mask,opt_watermark,save_path,args)

    

    
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='diffusion watermark')
    parser.add_argument('--run_name', default='test')
    parser.add_argument('--dataset', default='Stable-Diffusion-Prompts')
    parser.add_argument('--image_length', default=512, type=int)
    parser.add_argument('--model_id', default='stabilityai/stable-diffusion-2-1-base')
    parser.add_argument('--with_tracking', action='store_true')
    parser.add_argument('--num_images', default=1, type=int)
    parser.add_argument('--guidance_scale', default=7.5, type=float)
    parser.add_argument('--num_inference_steps', default=50, type=int)
    parser.add_argument('--test_num_inference_steps', default=None, type=int)
    parser.add_argument('--max_num_log_image', default=100, type=int)
    parser.add_argument('--gen_seed', default=0, type=int)

    # watermark
    parser.add_argument('--w_seed', default=999999, type=int)  # 999999
    parser.add_argument('--w_channel', default=0, type=int)
    parser.add_argument('--w_pattern', default='rand')
    parser.add_argument('--w_mask_shape', default='circle')
    parser.add_argument('--w_up_radius', default=30, type=int)  # 10
    parser.add_argument('--w_low_radius', default=5, type=int)  # 10
    parser.add_argument('--w_measurement', default='l1_complex')
    parser.add_argument('--w_injection', default='complex')
    parser.add_argument('--w_pattern_const', default=0, type=float)

    parser.add_argument('--data_root', required=True, help='Path to the clean dataset directory')

    args = parser.parse_args()

    if args.test_num_inference_steps is None:
        args.test_num_inference_steps = args.num_inference_steps
    
    main(args)
