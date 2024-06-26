import math
import os
from typing import Optional, Union

import cv2
import imageio
import moviepy.editor as mpy
import numpy as np
import torch
import torch.distributed as dist
import torchvision
from einops import rearrange
from PIL import Image
from tqdm import tqdm


# We recommend to use the following affinity score(motion magnitude)
# Also encourage to try to construct different score by yourself
RANGE_LIST = [
    [1.0, 0.9, 0.85, 0.85, 0.85, 0.8],  # 0 Small Motion
    [1.0, 0.8, 0.8, 0.8, 0.79, 0.78, 0.75],  # Moderate Motion
    [1.0, 0.8, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.6, 0.5, 0.5],  # Large Motion
    [1.0, 0.9, 0.85, 0.85, 0.85, 0.8, 0.8, 0.8, 0.8, 0.8, 0.8, 0.8, 0.85, 0.85, 0.9, 1.0],  # Loop
    [1.0, 0.8, 0.8, 0.8, 0.79, 0.78, 0.75, 0.75, 0.75, 0.75, 0.75, 0.78, 0.79, 0.8, 0.8, 1.0],  # Loop
    [1.0, 0.8, 0.7, 0.7, 0.7, 0.7, 0.6, 0.5, 0.5, 0.6, 0.7, 0.7, 0.7, 0.7, 0.8, 1.0],  # Loop
    [0.5, 0.2],  # Style Transfer Large Motion
    [0.5, 0.4, 0.4, 0.4, 0.35, 0.35, 0.3, 0.25, 0.2],  # Style Transfer Moderate Motion
    [0.5, 0.4, 0.4, 0.4, 0.35, 0.3],  # Style Transfer Candidate Small Motion
]


def zero_rank_print(s):
    if (not dist.is_initialized()) or (dist.is_initialized() and dist.get_rank() == 0):
        print("### " + s)


def save_videos_mp4(video: torch.Tensor, path: str, fps: int = 8):
    video = rearrange(video, "b c t h w -> t b c h w")
    num_frames, batch_size, channels, height, width = video.shape
    assert batch_size == 1, "Only support batch size == 1"
    video = video.squeeze(1)
    video = rearrange(video, "t c h w -> t h w c")

    def make_frame(t):
        frame_tensor = video[int(t * fps)]
        frame_np = (frame_tensor * 255).numpy().astype("uint8")
        return frame_np

    clip = mpy.VideoClip(make_frame, duration=num_frames / fps)
    clip.write_videofile(path, fps=fps, codec="libx264")


def save_videos_grid(videos: torch.Tensor, path: str, rescale=False, n_rows=6, fps=8):
    videos = rearrange(videos, "b c t h w -> t b c h w")
    outputs = []
    for x in videos:
        x = torchvision.utils.make_grid(x, nrow=n_rows)
        x = x.transpose(0, 1).transpose(1, 2).squeeze(-1)
        if rescale:
            x = (x + 1.0) / 2.0  # -1,1 -> 0,1
        x = torch.clamp((x * 255), 0, 255).numpy().astype(np.uint8)
        outputs.append(x)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    imageio.mimsave(path, outputs, fps=fps)


# DDIM Inversion
@torch.no_grad()
def init_prompt(prompt, pipeline):
    uncond_input = pipeline.tokenizer(
        [""], padding="max_length", max_length=pipeline.tokenizer.model_max_length, return_tensors="pt"
    )
    uncond_embeddings = pipeline.text_encoder(uncond_input.input_ids.to(pipeline.device))[0]
    text_input = pipeline.tokenizer(
        [prompt],
        padding="max_length",
        max_length=pipeline.tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_embeddings = pipeline.text_encoder(text_input.input_ids.to(pipeline.device))[0]
    context = torch.cat([uncond_embeddings, text_embeddings])

    return context


def next_step(
    model_output: Union[torch.FloatTensor, np.ndarray],
    timestep: int,
    sample: Union[torch.FloatTensor, np.ndarray],
    ddim_scheduler,
):
    timestep, next_timestep = (
        min(timestep - ddim_scheduler.config.num_train_timesteps // ddim_scheduler.num_inference_steps, 999),
        timestep,
    )
    alpha_prod_t = ddim_scheduler.alphas_cumprod[timestep] if timestep >= 0 else ddim_scheduler.final_alpha_cumprod
    alpha_prod_t_next = ddim_scheduler.alphas_cumprod[next_timestep]
    beta_prod_t = 1 - alpha_prod_t
    next_original_sample = (sample - beta_prod_t**0.5 * model_output) / alpha_prod_t**0.5
    next_sample_direction = (1 - alpha_prod_t_next) ** 0.5 * model_output
    next_sample = alpha_prod_t_next**0.5 * next_original_sample + next_sample_direction
    return next_sample


def get_noise_pred_single(latents, t, context, unet):
    noise_pred = unet(latents, t, encoder_hidden_states=context)["sample"]
    return noise_pred


@torch.no_grad()
def ddim_loop(pipeline, ddim_scheduler, latent, num_inv_steps, prompt):
    context = init_prompt(prompt, pipeline)
    uncond_embeddings, cond_embeddings = context.chunk(2)
    all_latent = [latent]
    latent = latent.clone().detach()
    for i in tqdm(range(num_inv_steps)):
        t = ddim_scheduler.timesteps[len(ddim_scheduler.timesteps) - i - 1]
        noise_pred = get_noise_pred_single(latent, t, cond_embeddings, pipeline.unet)
        latent = next_step(noise_pred, t, latent, ddim_scheduler)
        all_latent.append(latent)
    return all_latent


@torch.no_grad()
def ddim_inversion(pipeline, ddim_scheduler, video_latent, num_inv_steps, prompt=""):
    ddim_latents = ddim_loop(pipeline, ddim_scheduler, video_latent, num_inv_steps, prompt)
    return ddim_latents


def prepare_mask_coef(video_length: int, cond_frame: int, sim_range: list = [0.2, 1.0]):
    assert len(sim_range) == 2, "sim_range should has the length of 2, including the min and max similarity"

    assert video_length > 1, "video_length should be greater than 1"

    assert video_length > cond_frame, "video_length should be greater than cond_frame"

    diff = abs(sim_range[0] - sim_range[1]) / (video_length - 1)
    coef = [1.0] * video_length
    for f in range(video_length):
        f_diff = diff * abs(cond_frame - f)
        f_diff = 1 - f_diff
        coef[f] *= f_diff

    return coef


def prepare_mask_coef_by_statistics(video_length: int, cond_frame: int, sim_range: int):
    assert video_length > 0, "video_length should be greater than 0"

    assert video_length > cond_frame, "video_length should be greater than cond_frame"

    range_list = RANGE_LIST

    assert sim_range < len(range_list), f"sim_range type{sim_range} not implemented"

    coef = range_list[sim_range]
    coef = coef + ([coef[-1]] * (video_length - len(coef)))

    order = [abs(i - cond_frame) for i in range(video_length)]
    coef = [coef[order[i]] for i in range(video_length)]

    return coef


def prepare_mask_coef_multi_cond(video_length: int, cond_frames: list, sim_range: list = [0.2, 1.0]):
    assert len(sim_range) == 2, "sim_range should has the length of 2, including the min and max similarity"

    assert video_length > 1, "video_length should be greater than 1"

    assert isinstance(cond_frames, list), "cond_frames should be a list"

    assert video_length > max(cond_frames), "video_length should be greater than cond_frame"

    if max(sim_range) == min(sim_range):
        cond_coefs = [sim_range[0]] * video_length
        return cond_coefs

    cond_coefs = []

    for cond_frame in cond_frames:
        cond_coef = prepare_mask_coef(video_length, cond_frame, sim_range)
        cond_coefs.append(cond_coef)

    mixed_coef = [0] * video_length
    for conds in range(len(cond_frames)):
        for f in range(video_length):
            mixed_coef[f] = abs(cond_coefs[conds][f] - mixed_coef[f])

        if conds > 0:
            min_num = min(mixed_coef)
            max_num = max(mixed_coef)

            for f in range(video_length):
                mixed_coef[f] = (mixed_coef[f] - min_num) / (max_num - min_num)

    mixed_max = max(mixed_coef)
    mixed_min = min(mixed_coef)
    for f in range(video_length):
        mixed_coef[f] = (max(sim_range) - min(sim_range)) * (mixed_coef[f] - mixed_min) / (
            mixed_max - mixed_min
        ) + min(sim_range)

    mixed_coef = [
        x if min(sim_range) <= x <= max(sim_range) else min(sim_range) if x < min(sim_range) else max(sim_range)
        for x in mixed_coef
    ]

    return mixed_coef


def prepare_masked_latent_cond(video_length: int, cond_frames: list):
    for cond_frame in cond_frames:
        assert cond_frame < video_length, "cond_frame should be smaller than video_length"
        assert cond_frame > -1, f"cond_frame should be in the range of [0, {video_length}]"

    cond_frames.sort()
    nearest = [cond_frames[0]] * video_length
    for f in range(video_length):
        for cond_frame in cond_frames:
            if abs(nearest[f] - f) > abs(cond_frame - f):
                nearest[f] = cond_frame

    maked_latent_cond = nearest

    return maked_latent_cond


def estimated_kernel_size(frame_width: int, frame_height: int) -> int:
    """Estimate kernel size based on video resolution."""
    # TODO: This equation is based on manual estimation from a few videos.
    # Create a more comprehensive test suite to optimize against.
    size: int = 4 + round(math.sqrt(frame_width * frame_height) / 192)
    if size % 2 == 0:
        size += 1
    return size


def detect_edges(lum: np.ndarray) -> np.ndarray:
    """Detect edges using the luma channel of a frame.

    Arguments:
        lum: 2D 8-bit image representing the luma channel of a frame.

    Returns:
        2D 8-bit image of the same size as the input, where pixels with values of 255
        represent edges, and all other pixels are 0.
    """
    # Initialize kernel.
    kernel_size = estimated_kernel_size(lum.shape[1], lum.shape[0])
    kernel = np.ones((kernel_size, kernel_size), np.uint8)

    # Estimate levels for thresholding.
    # TODO(0.6.3): Add config file entries for sigma, aperture/kernel size, etc.
    sigma: float = 1.0 / 3.0
    median = np.median(lum)
    low = int(max(0, (1.0 - sigma) * median))
    high = int(min(255, (1.0 + sigma) * median))

    # Calculate edges using Canny algorithm, and reduce noise by dilating the edges.
    # This increases edge overlap leading to improved robustness against noise and slow
    # camera movement. Note that very large kernel sizes can negatively affect accuracy.
    edges = cv2.Canny(lum, low, high)
    return cv2.dilate(edges, kernel)


def prepare_mask_coef_by_score(
    video_shape: list,
    cond_frame_idx: list,
    sim_range: list = [0.2, 1.0],
    statistic: list = [1, 100],
    coef_max: int = 0.98,
    score: Optional[torch.Tensor] = None,
):
    """
    the shape of video_data is (b f c h w)
    cond_frame_idx is a list, with length of batch_size
    the shape of statistic  is (f 2)
    the shape of score      is (b f)
    the shape of coef       is (b f)
    """
    assert (
        len(video_shape) == 2
    ), f"the shape of video_shape should be (b f c h w), but now get {len(video_shape.shape)} channels"

    batch_size, frame_num = video_shape[0], video_shape[1]

    score = score.permute(0, 2, 1).squeeze(0)

    # list -> b 1
    cond_fram_mat = torch.tensor(cond_frame_idx).unsqueeze(-1)

    statistic = torch.tensor(statistic)
    # (f 2) -> (b f 2)
    statistic = statistic.repeat(batch_size, 1, 1)

    # shape of order (b f), shape of cond_mat (b f)
    order = torch.arange(0, frame_num, 1)
    order = order.repeat(batch_size, 1)
    cond_mat = torch.ones((batch_size, frame_num)) * cond_fram_mat
    order = abs(order - cond_mat)

    statistic = statistic[:, order.to(torch.long)][0, :, :, :]

    # score (b f) max_s (b f 1)
    max_stats = torch.max(statistic, dim=2).values.to(dtype=score.dtype)
    min_stats = torch.min(statistic, dim=2).values.to(dtype=score.dtype)

    score[score > max_stats] = max_stats[score > max_stats] * 0.95
    score[score < min_stats] = min_stats[score < min_stats]

    eps = 1e-10
    coef = 1 - abs((score / (max_stats + eps)) * (max(sim_range) - min(sim_range)))

    indices = torch.arange(coef.shape[0]).unsqueeze(1)
    coef[indices, cond_fram_mat] = 1.0

    return coef


def preprocess_img(img_path, max_size: int = 512):
    ori_image = Image.open(img_path).convert("RGB")

    width, height = ori_image.size

    long_edge = max(width, height)
    if long_edge > max_size:
        scale_factor = max_size / long_edge
    else:
        scale_factor = 1
    width = int(width * scale_factor)
    height = int(height * scale_factor)
    ori_image = ori_image.resize((width, height))

    if (width % 8 != 0) or (height % 8 != 0):
        in_width = (width // 8) * 8
        in_height = (height // 8) * 8
    else:
        in_width = width
        in_height = height
        in_image = ori_image

    in_image = ori_image.resize((in_width, in_height))
    # in_image = ori_image.resize((512, 512))
    in_image_np = np.array(in_image)
    return in_image_np, in_height, in_width
