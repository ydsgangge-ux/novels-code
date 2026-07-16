"""ComfyUI workflow templates.

Each function returns a standard ComfyUI API-format workflow dict
that can be submitted directly via POST /prompt.
"""

from __future__ import annotations

import random
from typing import Any


def build_txt2img_workflow(
    checkpoint: str,
    prompt: str,
    negative_prompt: str = "blurry, low quality",
    width: int = 512,
    height: int = 512,
    steps: int = 25,
    cfg_scale: float = 7.0,
    sampler: str = "dpmpp_2m",
    scheduler: str = "karras",
    seed: int = -1,
) -> dict[str, Any]:
    if seed == -1:
        seed = random.randint(0, 2**32 - 1)

    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["4", 1]},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg_scale,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "gangge"},
        },
    }


def build_txt2img_with_lora(
    checkpoint: str,
    lora_name: str,
    lora_strength: float = 0.8,
    prompt: str = "",
    negative_prompt: str = "blurry, low quality",
    width: int = 512,
    height: int = 512,
    steps: int = 25,
    cfg_scale: float = 7.0,
    sampler: str = "dpmpp_2m",
    scheduler: str = "karras",
    seed: int = -1,
) -> dict[str, Any]:
    """txt2img workflow with LoRA loaded.

    Node graph:
      4  CheckpointLoaderSimple
      10 LoraLoader (model+clip from 4)
      6  CLIPTextEncode (positive, clip from 10)
      7  CLIPTextEncode (negative, clip from 10)
      5  EmptyLatentImage
      3  KSampler (model from 10)
      8  VAEDecode (vae from 4)
      9  SaveImage
    """
    if seed == -1:
        seed = random.randint(0, 2**32 - 1)

    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "10": {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["4", 0],
                "clip": ["4", 1],
                "lora_name": lora_name,
                "strength_model": lora_strength,
                "strength_clip": lora_strength,
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["10", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["10", 1]},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["10", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg_scale,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "gangge_lora"},
        },
    }


def build_img2img_workflow(
    checkpoint: str,
    input_image_base64: str,
    prompt: str,
    negative_prompt: str = "blurry, low quality",
    denoise: float = 0.75,
    steps: int = 25,
    cfg_scale: float = 7.0,
    sampler: str = "dpmpp_2m",
    scheduler: str = "karras",
    seed: int = -1,
) -> dict[str, Any]:
    if seed == -1:
        seed = random.randint(0, 2**32 - 1)

    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "10": {
            "class_type": "ETN_LoadImageBase64",
            "inputs": {"image": input_image_base64},
        },
        "11": {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["10", 0], "vae": ["4", 2]},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["4", 1]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["11", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg_scale,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": denoise,
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "gangge_i2i"},
        },
    }
