import os
import torch
import math
from tqdm import tqdm

from accelerate import init_empty_weights
from accelerate.utils import set_module_tensor_to_device

import folder_paths
import comfy.model_management as mm
from comfy.utils import load_torch_file, ProgressBar, common_upscale
import comfy.model_base
import comfy.latent_formats
from comfy.cli_args import args, LatentPreviewMethod

from .utils import log

script_directory = os.path.dirname(os.path.abspath(__file__))
vae_scaling_factor = 0.476986

from .diffusers_helper.models.hunyuan_video_packed import HunyuanVideoTransformer3DModel
from .diffusers_helper.memory import DynamicSwapInstaller, move_model_to_device_with_memory_preservation
from .diffusers_helper.pipelines.k_diffusion_hunyuan import sample_hunyuan
from .diffusers_helper.utils import crop_or_pad_yield_mask
from .diffusers_helper.bucket_tools import find_nearest_bucket

from diffusers.loaders.lora_conversion_utils import _convert_hunyuan_video_lora_to_diffusers

class HyVideoModel(comfy.model_base.BaseModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pipeline = {}
        self.load_device = mm.get_torch_device()

    def __getitem__(self, k):
        return self.pipeline[k]

    def __setitem__(self, k, v):
        self.pipeline[k] = v


class HyVideoModelConfig:
    def __init__(self, dtype):
        self.unet_config = {}
        self.unet_extra_config = {}
        self.latent_format = comfy.latent_formats.HunyuanVideo
        self.latent_format.latent_channels = 16
        self.manual_cast_dtype = dtype
        self.sampling_settings = {"multiplier": 1.0}
        self.memory_usage_factor = 2.0
        self.unet_config["disable_unet_model_creation"] = True

class FramePackTorchCompileSettings:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "backend": (["inductor","cudagraphs"], {"default": "inductor"}),
                "fullgraph": ("BOOLEAN", {"default": False, "tooltip": "Enable full graph mode"}),
                "mode": (["default", "max-autotune", "max-autotune-no-cudagraphs", "reduce-overhead"], {"default": "default"}),
                "dynamic": ("BOOLEAN", {"default": False, "tooltip": "Enable dynamic mode"}),
                "dynamo_cache_size_limit": ("INT", {"default": 64, "min": 0, "max": 1024, "step": 1, "tooltip": "torch._dynamo.config.cache_size_limit"}),
                "compile_single_blocks": ("BOOLEAN", {"default": True, "tooltip": "Enable single block compilation"}),
                "compile_double_blocks": ("BOOLEAN", {"default": True, "tooltip": "Enable double block compilation"}),
            },
        }
    RETURN_TYPES = ("FRAMEPACKCOMPILEARGS",)
    RETURN_NAMES = ("torch_compile_args",)
    FUNCTION = "loadmodel"
    CATEGORY = "HunyuanVideoWrapper"
    DESCRIPTION = "torch.compile settings, when connected to the model loader, torch.compile of the selected layers is attempted. Requires Triton and torch 2.5.0 is recommended"

    def loadmodel(self, backend, fullgraph, mode, dynamic, dynamo_cache_size_limit, compile_single_blocks, compile_double_blocks):

        compile_args = {
            "backend": backend,
            "fullgraph": fullgraph,
            "mode": mode,
            "dynamic": dynamic,
            "dynamo_cache_size_limit": dynamo_cache_size_limit,
            "compile_single_blocks": compile_single_blocks,
            "compile_double_blocks": compile_double_blocks
        }

        return (compile_args, )

#region Model loading
class DownloadAndLoadFramePackModel:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": (["lllyasviel/FramePackI2V_HY"],),

            "base_precision": (["fp32", "bf16", "fp16"], {"default": "bf16"}),
            "quantization": (['disabled', 'fp8_e4m3fn', 'fp8_e4m3fn_fast', 'fp8_e5m2'], {"default": 'disabled', "tooltip": "optional quantization method"}),
            },
            "optional": {
                "attention_mode": ([
                    "sdpa",
                    "flash_attn",
                    "sageattn",
                    ], {"default": "sdpa"}),
                "compile_args": ("FRAMEPACKCOMPILEARGS", ),
            }
        }

    RETURN_TYPES = ("FramePackMODEL",)
    RETURN_NAMES = ("model", )
    FUNCTION = "loadmodel"
    CATEGORY = "FramePackWrapper"

    def loadmodel(self, model, base_precision, quantization,
                  compile_args=None, attention_mode="sdpa"):

        base_dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "fp8_e4m3fn_fast": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp16_fast": torch.float16, "fp32": torch.float32}[base_precision]

        device = mm.get_torch_device()

        model_path = os.path.join(folder_paths.models_dir, "diffusers", "lllyasviel", "FramePackI2V_HY")
        if not os.path.exists(model_path):
            print(f"Downloading clip model to: {model_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=model,
                local_dir=model_path,
                local_dir_use_symlinks=False,
            )

        transformer = HunyuanVideoTransformer3DModel.from_pretrained(model_path, torch_dtype=base_dtype, attention_mode=attention_mode).cpu()
        params_to_keep = {"norm", "bias", "time_in", "vector_in", "guidance_in", "txt_in", "img_in"}
        if quantization == 'fp8_e4m3fn' or quantization == 'fp8_e4m3fn_fast':
            transformer = transformer.to(torch.float8_e4m3fn)
            if quantization == "fp8_e4m3fn_fast":
                from .fp8_optimization import convert_fp8_linear
                convert_fp8_linear(transformer, base_dtype, params_to_keep=params_to_keep)
        elif quantization == 'fp8_e5m2':
            transformer = transformer.to(torch.float8_e5m2)
        else:
            transformer = transformer.to(base_dtype)

        DynamicSwapInstaller.install_model(transformer, device=device)

        if compile_args is not None:
            if compile_args["compile_single_blocks"]:
                for i, block in enumerate(transformer.single_transformer_blocks):
                    transformer.single_transformer_blocks[i] = torch.compile(block, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])
            if compile_args["compile_double_blocks"]:
                for i, block in enumerate(transformer.transformer_blocks):
                    transformer.transformer_blocks[i] = torch.compile(block, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])

            #transformer = torch.compile(transformer, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])

        pipe = {
            "transformer": transformer.eval(),
            "dtype": base_dtype,
        }
        return (pipe, )

class FramePackLoraSelect:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
               "lora": (folder_paths.get_filename_list("loras"),
                {"tooltip": "LORA models are expected to be in ComfyUI/models/loras with .safetensors extension"}),
                "strength": ("FLOAT", {"default": 1.0, "min": -10.0, "max": 10.0, "step": 0.0001, "tooltip": "LORA strength, set to 0.0 to unmerge the LORA"}),
                "fuse_lora": ("BOOLEAN", {"default": True, "tooltip": "Fuse the LORA model with the base model. This is recommended for better performance."}),
            },
            "optional": {
                "prev_lora":("FPLORA", {"default": None, "tooltip": "For loading multiple LoRAs"}),
            }
        }

    RETURN_TYPES = ("FPLORA",)
    RETURN_NAMES = ("lora", )
    FUNCTION = "getlorapath"
    CATEGORY = "FramePackWrapper"
    DESCRIPTION = "Select a LoRA model from ComfyUI/models/loras"

    def getlorapath(self, lora, strength, prev_lora=None, fuse_lora=True):
        loras_list = []

        lora = {
            "path": folder_paths.get_full_path("loras", lora),
            "strength": strength,
            "name": lora.split(".")[0],
            "fuse_lora": fuse_lora,
        }
        if prev_lora is not None:
            loras_list.extend(prev_lora)

        loras_list.append(lora)
        return (loras_list,)

class LoadFramePackModel:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": (folder_paths.get_filename_list("diffusion_models"), {"tooltip": "These models are loaded from the 'ComfyUI/models/diffusion_models' -folder",}),

            "base_precision": (["fp32", "bf16", "fp16"], {"default": "bf16"}),
            "quantization": (['disabled', 'fp8_e4m3fn', 'fp8_e4m3fn_fast', 'fp8_e5m2'], {"default": 'disabled', "tooltip": "optional quantization method"}),
            "load_device": (["main_device", "offload_device"], {"default": "cuda", "tooltip": "Initialize the model on the main device or offload device"}),
            },
            "optional": {
                "attention_mode": ([
                    "sdpa",
                    "flash_attn",
                    "sageattn",
                    ], {"default": "sdpa"}),
                "compile_args": ("FRAMEPACKCOMPILEARGS", ),
                "lora": ("FPLORA", {"default": None, "tooltip": "LORA model to load"}),
            }
        }

    RETURN_TYPES = ("FramePackMODEL",)
    RETURN_NAMES = ("model", )
    FUNCTION = "loadmodel"
    CATEGORY = "FramePackWrapper"

    def loadmodel(self, model, base_precision, quantization,
                  compile_args=None, attention_mode="sdpa", lora=None, load_device="main_device"):

        base_dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "fp8_e4m3fn_fast": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp16_fast": torch.float16, "fp32": torch.float32}[base_precision]

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        if load_device == "main_device":
            transformer_load_device = device
        else:
            transformer_load_device = offload_device

        model_path = folder_paths.get_full_path_or_raise("diffusion_models", model)
        model_config_path = os.path.join(script_directory, "transformer_config.json")
        import json
        with open(model_config_path, "r") as f:
            config = json.load(f)
        sd = load_torch_file(model_path, device=offload_device, safe_load=True)
        model_weight_dtype = sd['single_transformer_blocks.0.attn.to_k.weight'].dtype

        with init_empty_weights():
            transformer = HunyuanVideoTransformer3DModel(**config, attention_mode=attention_mode)

        params_to_keep = {"norm", "bias", "time_in", "vector_in", "guidance_in", "txt_in", "img_in"}
        if quantization == "fp8_e4m3fn" or quantization == "fp8_e4m3fn_fast" or quantization == "fp8_scaled":
            dtype = torch.float8_e4m3fn
        elif quantization == "fp8_e5m2":
            dtype = torch.float8_e5m2
        else:
            dtype = base_dtype

        if lora is not None:
            after_lora_dtype = dtype
            dtype = base_dtype

        print("Using accelerate to load and assign model weights to device...")
        param_count = sum(1 for _ in transformer.named_parameters())
        for name, param in tqdm(transformer.named_parameters(),
                desc=f"Loading transformer parameters to {transformer_load_device}",
                total=param_count,
                leave=True):
            dtype_to_use = base_dtype if any(keyword in name for keyword in params_to_keep) else dtype

            set_module_tensor_to_device(transformer, name, device=transformer_load_device, dtype=dtype_to_use, value=sd[name])

        if lora is not None:
            adapter_list = []
            adapter_weights = []

            for l in lora:
                fuse = True if l["fuse_lora"] else False
                lora_sd = load_torch_file(l["path"])

                if "lora_unet_single_transformer_blocks_0_attn_to_k.lora_up.weight" in lora_sd:
                    from .utils import convert_to_diffusers
                    lora_sd = convert_to_diffusers("lora_unet_", lora_sd)

                if not "transformer.single_transformer_blocks.0.attn_to.k.lora_A.weight" in lora_sd:
                    log.info(f"Converting LoRA weights from {l['path']} to diffusers format...")
                    lora_sd = _convert_hunyuan_video_lora_to_diffusers(lora_sd)

                lora_rank = None
                for key, val in lora_sd.items():
                    if "lora_B" in key or "lora_up" in key:
                        lora_rank = val.shape[1]
                        break
                if lora_rank is not None:
                    log.info(f"Merging rank {lora_rank} LoRA weights from {l['path']} with strength {l['strength']}")
                    adapter_name = l['path'].split("/")[-1].split(".")[0]
                    adapter_weight = l['strength']
                    transformer.load_lora_adapter(lora_sd, weight_name=l['path'].split("/")[-1], lora_rank=lora_rank, adapter_name=adapter_name)

                    adapter_list.append(adapter_name)
                    adapter_weights.append(adapter_weight)

                del lora_sd
                mm.soft_empty_cache()
            if adapter_list:
                transformer.set_adapters(adapter_list, weights=adapter_weights)
                if fuse:
                    if model_weight_dtype not in [torch.float32, torch.float16, torch.bfloat16]:
                        raise ValueError("Fusing LoRA doesn't work well with fp8 model weights. Please use a bf16 model file, or disable LoRA fusing.")
                    lora_scale = 1
                    transformer.fuse_lora(lora_scale=lora_scale)
                    transformer.delete_adapters(adapter_list)

            if quantization == "fp8_e4m3fn" or quantization == "fp8_e4m3fn_fast" or quantization == "fp8_e5m2":
                params_to_keep = {"norm", "bias", "time_in", "vector_in", "guidance_in", "txt_in", "img_in"}
                for name, param in transformer.named_parameters():
                    # Make sure to not cast the LoRA weights to fp8.
                    if not any(keyword in name for keyword in params_to_keep) and not 'lora' in name:
                        param.data = param.data.to(after_lora_dtype)

        if quantization == "fp8_e4m3fn_fast":
            from .fp8_optimization import convert_fp8_linear
            convert_fp8_linear(transformer, base_dtype, params_to_keep=params_to_keep)


        DynamicSwapInstaller.install_model(transformer, device=device)

        if compile_args is not None:
            if compile_args["compile_single_blocks"]:
                for i, block in enumerate(transformer.single_transformer_blocks):
                    transformer.single_transformer_blocks[i] = torch.compile(block, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])
            if compile_args["compile_double_blocks"]:
                for i, block in enumerate(transformer.transformer_blocks):
                    transformer.transformer_blocks[i] = torch.compile(block, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])

            #transformer = torch.compile(transformer, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])

        pipe = {
            "transformer": transformer.eval(),
            "dtype": base_dtype,
        }
        return (pipe, )

class FramePackFindNearestBucket:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image": ("IMAGE", {"tooltip": "Image to resize"}),
            "base_resolution": ("INT", {"default": 640, "min": 64, "max": 2048, "step": 16, "tooltip": "Width of the image to encode"}),
            },
        }

    RETURN_TYPES = ("INT", "INT", )
    RETURN_NAMES = ("width","height",)
    FUNCTION = "process"
    CATEGORY = "FramePackWrapper"
    DESCRIPTION = "Finds the closes resolution bucket as defined in the orignal code"

    def process(self, image, base_resolution):

        H, W = image.shape[1], image.shape[2]

        new_height, new_width = find_nearest_bucket(H, W, resolution=base_resolution)

        return (new_width, new_height, )


class FramePackSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("FramePackMODEL",),
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "start_latent": ("LATENT", {"tooltip": "init Latents to use for image2video"} ),
                "steps": ("INT", {"default": 30, "min": 1}),
                "use_teacache": ("BOOLEAN", {"default": True, "tooltip": "Use teacache for faster sampling."}),
                "teacache_rel_l1_thresh": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "The threshold for the relative L1 loss."}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "guidance_scale": ("FLOAT", {"default": 10.0, "min": 0.0, "max": 32.0, "step": 0.01}),
                "shift": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1000.0, "step": 0.01}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "latent_window_size": ("INT", {"default": 9, "min": 1, "max": 33, "step": 1, "tooltip": "The size of the latent window to use for sampling."}),
                "total_second_length": ("FLOAT", {"default": 5, "min": 0.1, "max": 120, "step": 0.1, "tooltip": "The total length of the video in seconds."}),
                "gpu_memory_preservation": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 128.0, "step": 0.1, "tooltip": "The amount of GPU memory to preserve."}),
                "sampler": (["unipc_bh1", "unipc_bh2"],
                    {
                        "default": 'unipc_bh1'
                    }),
            },
            "optional": {
                "image_embeds": ("CLIP_VISION_OUTPUT", ),
                "end_latent": ("LATENT", {"tooltip": "end Latents to use for image2video"} ),
                "end_image_embeds": ("CLIP_VISION_OUTPUT", {"tooltip": "end Image's clip embeds"} ),
                "embed_interpolation": (["disabled", "weighted_average", "linear"], {"default": 'disabled', "tooltip": "Image embedding interpolation type. If linear, will smoothly interpolate with time, else it'll be weighted average with the specified weight."}),
                "start_embed_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Weighted average constant for image embed interpolation. If end image is not set, the embed's strength won't be affected"}),
                "initial_samples": ("LATENT", {"tooltip": "init Latents to use for video2video"} ),
                "denoise_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            }
        }

    RETURN_TYPES = ("LATENT", )
    RETURN_NAMES = ("samples",)
    FUNCTION = "process"
    CATEGORY = "FramePackWrapper"

    def process(self, model, shift, positive, negative, latent_window_size, use_teacache, total_second_length, teacache_rel_l1_thresh, steps, cfg,
                guidance_scale, seed, sampler, gpu_memory_preservation, start_latent=None, image_embeds=None, end_latent=None, end_image_embeds=None, embed_interpolation="linear", start_embed_strength=1.0, initial_samples=None, denoise_strength=1.0):
        total_latent_sections = (total_second_length * 30) / (latent_window_size * 4)
        total_latent_sections = int(max(round(total_latent_sections), 1))
        print("total_latent_sections: ", total_latent_sections)

        transformer = model["transformer"]
        base_dtype = model["dtype"]

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        mm.unload_all_models()
        mm.cleanup_models()
        mm.soft_empty_cache()

        if start_latent is not None:
            start_latent = start_latent["samples"] * vae_scaling_factor
        if initial_samples is not None:
            initial_samples = initial_samples["samples"] * vae_scaling_factor
        if end_latent is not None:
            end_latent = end_latent["samples"] * vae_scaling_factor
        has_end_image = end_latent is not None
        print("start_latent", start_latent.shape)
        B, C, T, H, W = start_latent.shape

        if image_embeds is not None:
            start_image_encoder_last_hidden_state = image_embeds["last_hidden_state"].to(device, base_dtype)

        if has_end_image:
            assert end_image_embeds is not None
            end_image_encoder_last_hidden_state = end_image_embeds["last_hidden_state"].to(device, base_dtype)
        else:
            if image_embeds is not None:
                end_image_encoder_last_hidden_state = torch.zeros_like(start_image_encoder_last_hidden_state)

        llama_vec = positive[0][0].to(device, base_dtype)
        clip_l_pooler = positive[0][1]["pooled_output"].to(device, base_dtype)

        if not math.isclose(cfg, 1.0):
            llama_vec_n = negative[0][0].to(device, base_dtype)
            clip_l_pooler_n = negative[0][1]["pooled_output"].to(device, base_dtype)
        else:
            llama_vec_n = torch.zeros_like(llama_vec, device=device)
            clip_l_pooler_n = torch.zeros_like(clip_l_pooler, device=device)

        llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
        llama_vec_n, llama_attention_mask_n = crop_or_pad_yield_mask(llama_vec_n, length=512)


        # Sampling

        rnd = torch.Generator("cpu").manual_seed(seed)

        num_frames = latent_window_size * 4 - 3

        history_latents = torch.zeros(size=(1, 16, 1 + 2 + 16, H, W), dtype=torch.float32).cpu()

        total_generated_latent_frames = 0

        latent_paddings_list = list(reversed(range(total_latent_sections)))
        latent_paddings = latent_paddings_list.copy()  # Create a copy for iteration

        comfy_model = HyVideoModel(
                HyVideoModelConfig(base_dtype),
                model_type=comfy.model_base.ModelType.FLOW,
                device=device,
            )

        patcher = comfy.model_patcher.ModelPatcher(comfy_model, device, torch.device("cpu"))
        from latent_preview import prepare_callback
        callback = prepare_callback(patcher, steps)

        move_model_to_device_with_memory_preservation(transformer, target_device=device, preserved_memory_gb=gpu_memory_preservation)

        if total_latent_sections > 4:
            # In theory the latent_paddings should follow the above sequence, but it seems that duplicating some
            # items looks better than expanding it when total_latent_sections > 4
            # One can try to remove below trick and just
            # use `latent_paddings = list(reversed(range(total_latent_sections)))` to compare
            latent_paddings = [3] + [2] * (total_latent_sections - 3) + [1, 0]
            latent_paddings_list = latent_paddings.copy()

        for i, latent_padding in enumerate(latent_paddings):
            print(f"latent_padding: {latent_padding}")
            is_last_section = latent_padding == 0
            is_first_section = latent_padding == latent_paddings[0]
            latent_padding_size = latent_padding * latent_window_size

            if image_embeds is not None:
                if embed_interpolation != "disabled":
                    if embed_interpolation == "linear":
                        if total_latent_sections <= 1:
                            frac = 1.0  # Handle case with only one section
                        else:
                            frac = 1 - i / (total_latent_sections - 1)  # going backwards
                    else:
                        frac = start_embed_strength if has_end_image else 1.0

                    image_encoder_last_hidden_state = start_image_encoder_last_hidden_state * frac + (1 - frac) * end_image_encoder_last_hidden_state
                else:
                    image_encoder_last_hidden_state = start_image_encoder_last_hidden_state * start_embed_strength
            else:
                image_encoder_last_hidden_state = None

            print(f'latent_padding_size = {latent_padding_size}, is_last_section = {is_last_section}, is_first_section = {is_first_section}')

            start_latent_frames = T  # 0 or 1
            indices = torch.arange(0, sum([start_latent_frames, latent_padding_size, latent_window_size, 1, 2, 16])).unsqueeze(0)
            clean_latent_indices_pre, blank_indices, latent_indices, clean_latent_indices_post, clean_latent_2x_indices, clean_latent_4x_indices = indices.split([start_latent_frames, latent_padding_size, latent_window_size, 1, 2, 16], dim=1)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)

            clean_latents_pre = start_latent.to(history_latents)
            clean_latents_post, clean_latents_2x, clean_latents_4x = history_latents[:, :, :1 + 2 + 16, :, :].split([1, 2, 16], dim=2)
            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            # Use end image latent for the first section if provided
            if has_end_image and is_first_section:
                clean_latents_post = end_latent.to(history_latents)
                clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            #vid2vid WIP

            if initial_samples is not None:
                total_length = initial_samples.shape[2]

                # Get the max padding value for normalization
                max_padding = max(latent_paddings_list)

                if is_last_section:
                    # Last section should capture the end of the sequence
                    start_idx = max(0, total_length - latent_window_size)
                else:
                    # Calculate windows that distribute more evenly across the sequence
                    # This normalizes the padding values to create appropriate spacing
                    if max_padding > 0:  # Avoid division by zero
                        progress = (max_padding - latent_padding) / max_padding
                        start_idx = int(progress * max(0, total_length - latent_window_size))
                    else:
                        start_idx = 0

                end_idx = min(start_idx + latent_window_size, total_length)
                print(f"start_idx: {start_idx}, end_idx: {end_idx}, total_length: {total_length}")
                input_init_latents = initial_samples[:, :, start_idx:end_idx, :, :].to(device)


            if use_teacache:
                transformer.initialize_teacache(enable_teacache=True, num_steps=steps, rel_l1_thresh=teacache_rel_l1_thresh)
            else:
                transformer.initialize_teacache(enable_teacache=False)

            with torch.autocast(device_type=mm.get_autocast_device(device), dtype=base_dtype, enabled=True):
                generated_latents = sample_hunyuan(
                    transformer=transformer,
                    sampler=sampler,
                    initial_latent=input_init_latents if initial_samples is not None else None,
                    strength=denoise_strength,
                    width=W * 8,
                    height=H * 8,
                    frames=num_frames,
                    real_guidance_scale=cfg,
                    distilled_guidance_scale=guidance_scale,
                    guidance_rescale=0,
                    shift=shift if shift != 0 else None,
                    num_inference_steps=steps,
                    generator=rnd,
                    prompt_embeds=llama_vec,
                    prompt_embeds_mask=llama_attention_mask,
                    prompt_poolers=clip_l_pooler,
                    negative_prompt_embeds=llama_vec_n,
                    negative_prompt_embeds_mask=llama_attention_mask_n,
                    negative_prompt_poolers=clip_l_pooler_n,
                    device=device,
                    dtype=base_dtype,
                    image_embeddings=image_encoder_last_hidden_state,
                    latent_indices=latent_indices,
                    clean_latents=clean_latents,
                    clean_latent_indices=clean_latent_indices,
                    clean_latents_2x=clean_latents_2x,
                    clean_latent_2x_indices=clean_latent_2x_indices,
                    clean_latents_4x=clean_latents_4x,
                    clean_latent_4x_indices=clean_latent_4x_indices,
                    callback=callback,
                )

            if is_last_section:
                generated_latents = torch.cat([start_latent.to(generated_latents), generated_latents], dim=2)

            total_generated_latent_frames += int(generated_latents.shape[2])
            history_latents = torch.cat([generated_latents.to(history_latents), history_latents], dim=2)

            real_history_latents = history_latents[:, :, :total_generated_latent_frames, :, :]

            if is_last_section:
                break

        transformer.to(offload_device)
        mm.soft_empty_cache()

        return {"samples": real_history_latents / vae_scaling_factor},

class FramePackSingleFrameSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("FramePackMODEL",),
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "start_latent": ("LATENT", {"tooltip": "init Latents to use for image2image"}),
                "steps": ("INT", {"default": 30, "min": 1}),
                "use_teacache": ("BOOLEAN", {"default": True, "tooltip": "Use teacache for faster sampling."}),
                "teacache_rel_l1_thresh": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Threshold for relative L1 loss"}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "guidance_scale": ("FLOAT", {"default": 10.0, "min": 0.0, "max": 32.0, "step": 0.01}),
                "shift": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1000.0, "step": 0.01}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "latent_window_size": ("INT", {"default": 9, "min": 1, "max": 33, "step": 1, "tooltip": "Size of latent window for sampling"}),
                "gpu_memory_preservation": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 128.0, "step": 0.1, "tooltip": "GPU memory to preserve"}),
                "sampler": (["unipc_bh1", "unipc_bh2"], {"default": "unipc_bh1"}),
                "use_kisekaeichi": ("BOOLEAN", {"default": False, "tooltip": "Enable Kisekaeichi mode for style transfer"}),
            },
            "optional": {
                "image_embeds": ("CLIP_VISION_OUTPUT",),
                "initial_samples": ("LATENT", {"tooltip": "init Latents to use for image2image variation"}),
                "denoise_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "reference_latent": ("LATENT", {"tooltip": "Reference image latent for kisekaeichi mode"}),
                "reference_image_embeds": ("CLIP_VISION_OUTPUT", {"tooltip": "Reference image CLIP embeds for kisekaeichi mode"}),
                "target_index": ("INT", {"default": 1, "min": 0, "max": 8, "step": 1, "tooltip": "Target index for kisekaeichi (recommended: 1)"}),
                "history_index": ("INT", {"default": 13, "min": 0, "max": 16, "step": 1, "tooltip": "History index (recommended: 13)"}),
                "input_mask": ("MASK", {"tooltip": "Input mask for selective application"}),
                "reference_mask": ("MASK", {"tooltip": "Reference mask for selective features"}),
            },
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "process"
    CATEGORY = "FramePackWrapper"
    DESCRIPTION = "Single frame sampler with Kisekaeichi (style transfer) support"

    def process(self, model, shift, positive, negative, latent_window_size, use_teacache, teacache_rel_l1_thresh, steps, cfg, guidance_scale, seed,
        sampler, gpu_memory_preservation,start_latent=None, image_embeds=None, initial_samples=None, denoise_strength=1.0, use_kisekaeichi=False,
        reference_latent=None, reference_image_embeds=None, target_index=1, history_index=13, input_mask=None, reference_mask=None):

        transformer = model["transformer"]
        base_dtype = model["dtype"]
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        mm.unload_all_models()
        mm.cleanup_models()
        mm.soft_empty_cache()

        # Latent processing
        if start_latent is not None:
            start_latent = start_latent["samples"] * vae_scaling_factor
        if initial_samples is not None:
            initial_samples = initial_samples["samples"] * vae_scaling_factor
        if use_kisekaeichi and reference_latent is not None:
            reference_latent = reference_latent["samples"] * vae_scaling_factor
            log.info(f"Reference image latent shape: {reference_latent.shape}")

        log.info(f"start_latent shape {start_latent.shape}")
        B, C, T, H, W = start_latent.shape

        # image embeds
        if image_embeds is not None:
            start_image_encoder_last_hidden_state = image_embeds[
                "last_hidden_state"
            ].to(device, base_dtype)
        else:
            start_image_encoder_last_hidden_state = None

        if use_kisekaeichi and reference_image_embeds is not None:
            reference_image_encoder_last_hidden_state = reference_image_embeds["last_hidden_state"].to(device, base_dtype)
        else:
            reference_image_encoder_last_hidden_state = None

        # text embeds
        llama_vec = positive[0][0].to(device, base_dtype)
        clip_l_pooler = positive[0][1]["pooled_output"].to(device, base_dtype)

        if not math.isclose(cfg, 1.0):
            llama_vec_n = negative[0][0].to(device, base_dtype)
            clip_l_pooler_n = negative[0][1]["pooled_output"].to(device, base_dtype)
        else:
            llama_vec_n = torch.zeros_like(llama_vec, device=device)
            clip_l_pooler_n = torch.zeros_like(clip_l_pooler, device=device)

        llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
        llama_vec_n, llama_attention_mask_n = crop_or_pad_yield_mask(llama_vec_n, length=512)

        rnd = torch.Generator("cpu").manual_seed(seed)

        # hard coded single frame settings
        sample_num_frames = 1
        latent_padding = 0
        latent_padding_size = latent_padding * latent_window_size  # 0

        indices = torch.arange(
            0, sum([1, latent_padding_size, latent_window_size, 1, 2, 16])
        ).unsqueeze(0)
        split_sizes = [1, latent_padding_size, latent_window_size, 1, 2, 16]

        # Splitting when latent_padding_size is 0
        if latent_padding_size == 0:
            clean_latent_indices_pre = indices[:, 0:1]
            latent_indices = indices[:, 1 : 1 + latent_window_size]
            clean_latent_indices_post = indices[
                :, 1 + latent_window_size : 2 + latent_window_size
            ]
        else:
            (
                clean_latent_indices_pre,
                blank_indices,
                latent_indices,
                clean_latent_indices_post,
                clean_latent_2x_indices,
                clean_latent_4x_indices,
            ) = indices.split(split_sizes, dim=1)

        # one_frame_inference
        if use_kisekaeichi and reference_latent is not None:

            one_frame_inference = set()
            one_frame_inference.add(f"target_index={target_index}")
            one_frame_inference.add(f"history_index={history_index}")

            latent_indices = indices[:, -1:]  # Default is the last frame

            # Parameter analysis and processing
            for one_frame_param in one_frame_inference:
                if one_frame_param.startswith("target_index="):
                    target_idx = int(one_frame_param.split("=")[1])
                    latent_indices[:, 0] = target_idx
                    log.info(f"Setting latent_indices: target_index={target_idx}")

                elif one_frame_param.startswith("history_index="):
                    history_idx = int(one_frame_param.split("=")[1])
                    clean_latent_indices_post[:, 0] = history_idx
                    log.info(f"Setting clean_latent_indices_post: history_index={history_idx}")

            # dummy history_latents
            history_latents = torch.zeros(
                size=(1, 16, 1 + 2 + 16, H, W), dtype=torch.float32, device="cpu"
            )

            # Setting clean_latents_pre (input image)
            clean_latents_pre = start_latent.to(history_latents.dtype).to(
                history_latents.device
            )
            if len(clean_latents_pre.shape) < 5:
                clean_latents_pre = clean_latents_pre.unsqueeze(2)

            # Applying mask (input image)
            if input_mask is not None:
                height_latent, width_latent = clean_latents_pre.shape[-2:]
                input_mask_resized = (common_upscale(input_mask.unsqueeze(0).unsqueeze(0), width_latent,  height_latent, "bilinear", "center").squeeze(0).squeeze(0))
                input_mask_resized = input_mask_resized.to(clean_latents_pre.device)[None, None, None, :, :]
                clean_latents_pre = clean_latents_pre * input_mask_resized

            # Applying mask (reference image)
            if reference_mask is not None:
                height_latent, width_latent = clean_latents_post.shape[-2:]
                reference_mask_resized = (common_upscale(input_mask.unsqueeze(0).unsqueeze(0), width_latent,  height_latent, "bilinear", "center").squeeze(0).squeeze(0))
                reference_mask_resized = reference_mask_resized.to(clean_latents_post.device)[None, None, None, :, :]
                clean_latents_post = clean_latents_post * reference_mask_resized

            # Setting clean_latents
            clean_latents_post = (reference_latent[:, :, 0:1, :, :].to(history_latents))
            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)

            log.info("Kisekaeichi: 2x/4x indices disabled")

            # Processing image embeddings (utilizing both)
            if (
                reference_image_encoder_last_hidden_state is not None
                and start_image_encoder_last_hidden_state is not None
            ):
                ref_weight = 0.3
                input_weight = 1.0 - ref_weight
                image_encoder_last_hidden_state = (
                    start_image_encoder_last_hidden_state * input_weight
                    + reference_image_encoder_last_hidden_state * ref_weight
                )
                log.info(f"Image embeddings integrated (input:{input_weight:.2f}, reference:{ref_weight:.2f})")
            elif reference_image_encoder_last_hidden_state is not None:
                image_encoder_last_hidden_state = (reference_image_encoder_last_hidden_state)
            else:
                image_encoder_last_hidden_state = (start_image_encoder_last_hidden_state)

            log.info(f"Kisekaeichi setup complete:")
            log.info(f"  - clean_latents.shape: {clean_latents.shape} (input+reference)")
            log.info(f"  - latent_indices: {latent_indices}")
            log.info(f"  - clean_latent_indices: {clean_latent_indices}")
            log.info(f"  - sample_num_frames: {sample_num_frames}")
            log.info(f"  - 2x/4x disabled: True")

        else:
            # Normal mode (no reference image)
            all_indices = torch.arange(0, latent_window_size).unsqueeze(0)
            latent_indices = all_indices[:, -1:]

            clean_latents_pre = start_latent.to(torch.float32).cpu()
            if len(clean_latents_pre.shape) < 5:
                clean_latents_pre = clean_latents_pre.unsqueeze(2)

            clean_latents_post = torch.zeros_like(clean_latents_pre)
            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)

            # Index adjustment in normal mode
            clean_latent_indices = torch.tensor([[0]], dtype=clean_latent_indices.dtype, device=clean_latent_indices.device)
            clean_latents = clean_latents[:, :, :1, :, :]

            log.info("Kisekaeichi: 2x/4x indices disabled")

            image_encoder_last_hidden_state = start_image_encoder_last_hidden_state

            log.info("Normal mode settings:")
            log.info(f"  - clean_latents.shape: {clean_latents.shape}")
            log.info(f"  - latent_indices: {latent_indices}")
            log.info(f"  - clean_latent_indices: {clean_latent_indices}")

        # Processing initial samples
        input_init_latents = None
        if initial_samples is not None:
            input_init_latents = initial_samples[:, :, 0:1, :, :].to(device)

        # Comfy model config
        comfy_model = HyVideoModel(
            HyVideoModelConfig(base_dtype),
            model_type=comfy.model_base.ModelType.FLOW,
            device=device,
        )
        patcher = comfy.model_patcher.ModelPatcher(
            comfy_model, device, torch.device("cpu")
        )
        from latent_preview import prepare_callback

        callback = prepare_callback(patcher, steps)

        move_model_to_device_with_memory_preservation(
            transformer,
            target_device=device,
            preserved_memory_gb=gpu_memory_preservation,
        )

        if use_teacache:
            transformer.initialize_teacache(
                enable_teacache=True,
                num_steps=steps,
                rel_l1_thresh=teacache_rel_l1_thresh,
            )
        else:
            transformer.initialize_teacache(enable_teacache=False)

        with torch.autocast(device_type=mm.get_autocast_device(device), dtype=base_dtype, enabled=True):
            generated_latents = sample_hunyuan(
                transformer=transformer,
                sampler=sampler,
                initial_latent=input_init_latents,
                strength=denoise_strength,
                width=W * 8,
                height=H * 8,
                frames=sample_num_frames, 
                real_guidance_scale=cfg,
                distilled_guidance_scale=guidance_scale,
                guidance_rescale=0,
                shift=shift if shift != 0 else None,
                num_inference_steps=steps,
                generator=rnd,
                prompt_embeds=llama_vec,
                prompt_embeds_mask=llama_attention_mask,
                prompt_poolers=clip_l_pooler,
                negative_prompt_embeds=llama_vec_n,
                negative_prompt_embeds_mask=llama_attention_mask_n,
                negative_prompt_poolers=clip_l_pooler_n,
                device=device,
                dtype=base_dtype,
                image_embeddings=image_encoder_last_hidden_state,
                latent_indices=latent_indices,
                clean_latents=clean_latents,
                clean_latent_indices=clean_latent_indices,
                clean_latents_2x=None,
                clean_latent_2x_indices=None,
                clean_latents_4x=None,
                clean_latent_4x_indices=None,
                callback=callback,
            )

        transformer.to(offload_device)
        mm.soft_empty_cache()

        return ({"samples": generated_latents / vae_scaling_factor},)

# port from https://github.com/red-polo/FramePackLoop
class FramePackLoopSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("FramePackMODEL",),
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "start_latent": ("LATENT", {"tooltip": "init Latents to use for image2video"} ),
                "steps": ("INT", {"default": 30, "min": 1}),
                "use_teacache": ("BOOLEAN", {"default": True, "tooltip": "Use teacache for faster sampling."}),
                "teacache_rel_l1_thresh": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "The threshold for the relative L1 loss."}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "guidance_scale": ("FLOAT", {"default": 10.0, "min": 0.0, "max": 32.0, "step": 0.01}),
                "shift": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1000.0, "step": 0.01}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "latent_window_size": ("INT", {"default": 9, "min": 1, "max": 33, "step": 1, "tooltip": "The size of the latent window to use for sampling."}),
                "total_second_length": ("FLOAT", {"default": 5, "min": 0.1, "max": 120, "step": 0.1, "tooltip": "The total length of the video in seconds."}),
                "gpu_memory_preservation": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 128.0, "step": 0.1, "tooltip": "The amount of GPU memory to preserve."}),
                "sampler": (["unipc_bh1", "unipc_bh2"],
                    {
                        "default": 'unipc_bh1'
                    }),
            },
            "optional": {
                "image_embeds": ("CLIP_VISION_OUTPUT", ),
                "end_latent": ("LATENT", {"tooltip": "end Latents to use for image2video"} ),
                "end_image_embeds": ("CLIP_VISION_OUTPUT", {"tooltip": "end Image's clip embeds"} ),
                "embed_interpolation": (["disabled", "weighted_average", "linear"], {"default": 'disabled', "tooltip": "Image embedding interpolation type. If linear, will smoothly interpolate with time, else it'll be weighted average with the specified weight."}),
                "start_embed_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Weighted average constant for image embed interpolation. If end image is not set, the embed's strength won't be affected"}),
                "initial_samples": ("LATENT", {"tooltip": "init Latents to use for video2video"} ),
                "denoise_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "connection_second_length": ("FLOAT", {"default": 1.0, "min": 1, "max": 5, "step": 0.1, "tooltip": "The connection length of the video in seconds."}),
            }
        }

    RETURN_TYPES = ("LATENT", "INT", "INT")
    RETURN_NAMES = ("samples", "start_frames", "end_frames")
    FUNCTION = "process"
    CATEGORY = "FramePackWrapper"

    def process(self, model, shift, positive, negative, latent_window_size, use_teacache, total_second_length, teacache_rel_l1_thresh, steps, cfg,
                guidance_scale, seed, sampler, gpu_memory_preservation, start_latent=None, image_embeds=None, end_latent=None, end_image_embeds=None, embed_interpolation="linear", start_embed_strength=1.0, initial_samples=None, denoise_strength=1.0, connection_second_length=1.0):
        main_latent_sections = (total_second_length * 30) / (latent_window_size * 4)
        main_latent_sections = int(max(round(main_latent_sections), 1))
        connection_latent_sections = (connection_second_length * 30) / (latent_window_size * 4)
        connection_latent_sections = int(max(round(connection_second_length), 1))
        total_latent_sections = main_latent_sections + connection_latent_sections
        print("total_latent_sections: ", total_latent_sections)
        padding_second_length = 1

        transformer = model["transformer"]
        base_dtype = model["dtype"]

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        mm.unload_all_models()
        mm.cleanup_models()
        mm.soft_empty_cache()

        if start_latent is not None:
            start_latent = start_latent["samples"] * vae_scaling_factor
        if initial_samples is not None:
            initial_samples = initial_samples["samples"] * vae_scaling_factor
        if end_latent is not None:
            end_latent = end_latent["samples"] * vae_scaling_factor
        has_end_image = end_latent is not None
        print("start_latent", start_latent.shape)
        B, C, T, H, W = start_latent.shape

        if image_embeds is not None:
            start_image_encoder_last_hidden_state = image_embeds["last_hidden_state"].to(device, base_dtype)

        if has_end_image:
            assert end_image_embeds is not None
            end_image_encoder_last_hidden_state = end_image_embeds["last_hidden_state"].to(device, base_dtype)
        else:
            if image_embeds is not None:
                end_image_encoder_last_hidden_state = torch.zeros_like(start_image_encoder_last_hidden_state)

        llama_vec = positive[0][0].to(device, base_dtype)
        llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
        clip_l_pooler = positive[0][1]["pooled_output"].to(device, base_dtype)

        if not math.isclose(cfg, 1.0):
            llama_vec_n = negative[0][0].to(device, base_dtype)
            clip_l_pooler_n = negative[0][1]["pooled_output"].to(device, base_dtype)
        else:
            llama_vec_n = torch.zeros_like(llama_vec, device=device)
            clip_l_pooler_n = torch.zeros_like(clip_l_pooler, device=device)

        llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
        llama_vec_n, llama_attention_mask_n = crop_or_pad_yield_mask(llama_vec_n, length=512)


        # Sampling

        rnd = torch.Generator("cpu").manual_seed(seed)

        num_frames = latent_window_size * 4 - 3

        comfy_model = HyVideoModel(
                HyVideoModelConfig(base_dtype),
                model_type=comfy.model_base.ModelType.FLOW,
                device=device,
            )

        patcher = comfy.model_patcher.ModelPatcher(comfy_model, device, torch.device("cpu"))
        from latent_preview import prepare_callback
        callback = prepare_callback(patcher, steps)

        move_model_to_device_with_memory_preservation(transformer, target_device=device, preserved_memory_gb=gpu_memory_preservation)

        ##メイン作成

        history_latents = torch.zeros(size=(1, 16, 1 + 2 + 16, H, W), dtype=torch.float32).cpu()

        total_generated_latent_frames = 0

        latent_paddings_list = list(reversed(range(main_latent_sections)))
        latent_paddings = latent_paddings_list.copy()  # Create a copy for iteration

        if main_latent_sections > 4:
            # In theory the latent_paddings should follow the above sequence, but it seems that duplicating some
            # items looks better than expanding it when total_latent_sections > 4
            # One can try to remove below trick and just
            # use `latent_paddings = list(reversed(range(total_latent_sections)))` to compare
            latent_paddings = [3] + [2] * (main_latent_sections - 3) + [1, 0]
            latent_paddings_list = latent_paddings.copy()

        for i, latent_padding in enumerate(latent_paddings):
            print(f"latent_padding: {latent_padding}")
            is_last_section = latent_padding == 0
            is_first_section = latent_padding == latent_paddings[0]
            latent_padding_init_size = int(padding_second_length * latent_window_size)

            latent_padding_size = (latent_padding * latent_window_size) + latent_padding_init_size


            if image_embeds is not None:
                if embed_interpolation != "disabled":
                    if embed_interpolation == "linear":
                        if main_latent_sections <= 1:
                            frac = 1.0  # Handle case with only one section
                        else:
                            frac = 1 - i / (main_latent_sections - 1)  # going backwards
                    else:
                        frac = start_embed_strength if has_end_image else 1.0

                    image_encoder_last_hidden_state = start_image_encoder_last_hidden_state * frac + (1 - frac) * end_image_encoder_last_hidden_state
                else:
                    image_encoder_last_hidden_state = start_image_encoder_last_hidden_state * start_embed_strength
            else:
                image_encoder_last_hidden_state = None

            print(f'latent_padding_size = {latent_padding_size}, is_last_section = {is_last_section}, is_first_section = {is_first_section}')

            start_latent_frames = T  # 0 or 1
            indices = torch.arange(0, sum([start_latent_frames, latent_padding_size, latent_window_size, 1, 2, 16])).unsqueeze(0)
            clean_latent_indices_pre, blank_indices, latent_indices, clean_latent_indices_post, clean_latent_2x_indices, clean_latent_4x_indices = indices.split([start_latent_frames, latent_padding_size, latent_window_size, 1, 2, 16], dim=1)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)

            clean_latents_pre = start_latent.to(history_latents)
            clean_latents_post, clean_latents_2x, clean_latents_4x = history_latents[:, :, :1 + 2 + 16, :, :].split([1, 2, 16], dim=2)
            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            # Use end image latent for the first section if provided
            if has_end_image and is_first_section:
                clean_latents_post = end_latent.to(history_latents)
                clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            #vid2vid WIP

            if initial_samples is not None:
                total_length = initial_samples.shape[2]

                # Get the max padding value for normalization
                max_padding = max(latent_paddings_list)

                if is_last_section:
                    # Last section should capture the end of the sequence
                    start_idx = max(0, total_length - latent_window_size)
                else:
                    # Calculate windows that distribute more evenly across the sequence
                    # This normalizes the padding values to create appropriate spacing
                    if max_padding > 0:  # Avoid division by zero
                        progress = (max_padding - latent_padding) / max_padding
                        start_idx = int(progress * max(0, total_length - latent_window_size))
                    else:
                        start_idx = 0

                end_idx = min(start_idx + latent_window_size, total_length)
                print(f"start_idx: {start_idx}, end_idx: {end_idx}, total_length: {total_length}")
                input_init_latents = initial_samples[:, :, start_idx:end_idx, :, :].to(device)


            if use_teacache:
                transformer.initialize_teacache(enable_teacache=True, num_steps=steps, rel_l1_thresh=teacache_rel_l1_thresh)
            else:
                transformer.initialize_teacache(enable_teacache=False)

            with torch.autocast(device_type=mm.get_autocast_device(device), dtype=base_dtype, enabled=True):
                generated_latents = sample_hunyuan(
                    transformer=transformer,
                    sampler=sampler,
                    initial_latent=input_init_latents if initial_samples is not None else None,
                    strength=denoise_strength,
                    width=W * 8,
                    height=H * 8,
                    frames=num_frames,
                    real_guidance_scale=cfg,
                    distilled_guidance_scale=guidance_scale,
                    guidance_rescale=0,
                    shift=shift if shift != 0 else None,
                    num_inference_steps=steps,
                    generator=rnd,
                    prompt_embeds=llama_vec,
                    prompt_embeds_mask=llama_attention_mask,
                    prompt_poolers=clip_l_pooler,
                    negative_prompt_embeds=llama_vec_n,
                    negative_prompt_embeds_mask=llama_attention_mask_n,
                    negative_prompt_poolers=clip_l_pooler_n,
                    device=device,
                    dtype=base_dtype,
                    image_embeddings=image_encoder_last_hidden_state,
                    latent_indices=latent_indices,
                    clean_latents=clean_latents,
                    clean_latent_indices=clean_latent_indices,
                    clean_latents_2x=clean_latents_2x,
                    clean_latent_2x_indices=clean_latent_2x_indices,
                    clean_latents_4x=clean_latents_4x,
                    clean_latent_4x_indices=clean_latent_4x_indices,
                    callback=callback,
                )

            #if is_last_section:
            #    generated_latents = torch.cat([start_latent.to(generated_latents), generated_latents], dim=2)

            total_generated_latent_frames += int(generated_latents.shape[2])
            history_latents = torch.cat([generated_latents.to(history_latents), history_latents], dim=2)

            real_history_latents = history_latents[:, :, :total_generated_latent_frames, :, :]

            if is_last_section:
                break

        ##コネクション作成

        #post_history_latents = torch.zeros(size=(1, 16, 1 + 2 + 16, H, W), dtype=torch.float32).cpu()
        post_history_latents = history_latents[:, :, :total_generated_latent_frames, :, :]

        post_total_generated_latent_frames = total_generated_latent_frames

        latent_paddings_list = list(reversed(range(connection_latent_sections)))
        latent_paddings = latent_paddings_list.copy()  # Create a copy for iteration

        if connection_latent_sections > 4:
            # In theory the latent_paddings should follow the above sequence, but it seems that duplicating some
            # items looks better than expanding it when total_latent_sections > 4
            # One can try to remove below trick and just
            # use `latent_paddings = list(reversed(range(total_latent_sections)))` to compare
            latent_paddings = [3] + [2] * (connection_latent_sections - 3) + [1, 0]
            latent_paddings_list = latent_paddings.copy()

        if total_latent_sections > 2:
            N = 16
        elif total_latent_sections == 2:
            N= 15
        else:
            N=6

        for i, latent_padding in enumerate(latent_paddings):
            print(f"latent_padding: {latent_padding}")
            is_last_section = latent_padding == 0
            is_first_section = latent_padding == latent_paddings[0]
            latent_padding_size = latent_padding * latent_window_size

            indices = torch.arange(0, sum([1,latent_padding_size, latent_window_size, 1, 2, N])).unsqueeze(0)
            clean_latent_indices_pre, blank_indices, latent_indices, clean_latent_indices_post, clean_latent_2x_indices, clean_latent_4x_indices = indices.split([1,latent_padding_size, latent_window_size, 1, 2, N], dim=1)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)
            clean_latent_2x_indices = torch.cat([clean_latent_2x_indices], dim=1)
            clean_latent_4x_indices = torch.cat([clean_latent_4x_indices], dim=1)


            clean_latents_pre  = post_history_latents[:, :, -1:, :, :]
            clean_latents_post, clean_latents_2x, clean_latents_4x = post_history_latents[:, :, :1 + 2 + N, :, :].split([1, 2, N], dim=2)

            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)
            clean_latents_2x = torch.cat([clean_latents_2x], dim=2)
            clean_latents_4x = torch.cat([clean_latents_4x], dim=2)

            # Use end image latent for the first section if provided
            if has_end_image and is_first_section:
                clean_latents_post = end_latent.to(history_latents)
                clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            #vid2vid WIP

            if initial_samples is not None:
                total_length = initial_samples.shape[2]

                # Get the max padding value for normalization
                max_padding = max(latent_paddings_list)

                if is_last_section:
                    # Last section should capture the end of the sequence
                    start_idx = max(0, total_length - latent_window_size)
                else:
                    # Calculate windows that distribute more evenly across the sequence
                    # This normalizes the padding values to create appropriate spacing
                    if max_padding > 0:  # Avoid division by zero
                        progress = (max_padding - latent_padding) / max_padding
                        start_idx = int(progress * max(0, total_length - latent_window_size))
                    else:
                        start_idx = 0

                end_idx = min(start_idx + latent_window_size, total_length)
                print(f"start_idx: {start_idx}, end_idx: {end_idx}, total_length: {total_length}")
                input_init_latents = initial_samples[:, :, start_idx:end_idx, :, :].to(device)


            if use_teacache:
                transformer.initialize_teacache(enable_teacache=True, num_steps=steps, rel_l1_thresh=teacache_rel_l1_thresh)
            else:
                transformer.initialize_teacache(enable_teacache=False)

            with torch.autocast(device_type=mm.get_autocast_device(device), dtype=base_dtype, enabled=True):
                generated_latents = sample_hunyuan(
                    transformer=transformer,
                    sampler=sampler,
                    initial_latent=input_init_latents if initial_samples is not None else None,
                    strength=denoise_strength,
                    width=W * 8,
                    height=H * 8,
                    frames=num_frames,
                    real_guidance_scale=cfg,
                    distilled_guidance_scale=guidance_scale,
                    guidance_rescale=0,
                    shift=shift if shift != 0 else None,
                    num_inference_steps=steps,
                    generator=rnd,
                    prompt_embeds=llama_vec,
                    prompt_embeds_mask=llama_attention_mask,
                    prompt_poolers=clip_l_pooler,
                    negative_prompt_embeds=llama_vec_n,
                    negative_prompt_embeds_mask=llama_attention_mask_n,
                    negative_prompt_poolers=clip_l_pooler_n,
                    device=device,
                    dtype=base_dtype,
                    image_embeddings=image_encoder_last_hidden_state,
                    latent_indices=latent_indices,
                    clean_latents=clean_latents,
                    clean_latent_indices=clean_latent_indices,
                    clean_latents_2x=clean_latents_2x,
                    clean_latent_2x_indices=clean_latent_2x_indices,
                    clean_latents_4x=clean_latents_4x,
                    clean_latent_4x_indices=clean_latent_4x_indices,
                    callback=callback,
                )

            #if is_last_section:
            #    generated_latents = torch.cat([start_latent.to(generated_latents), generated_latents], dim=2)

            post_total_generated_latent_frames += int(generated_latents.shape[2])
            post_history_latents = torch.cat([generated_latents.to(post_history_latents), post_history_latents], dim=2)

            post_real_history_latents = post_history_latents[:, :, :post_total_generated_latent_frames, :, :]

            if is_last_section:
                break

        #1ループ作成
        connection_hisotry_latents = post_real_history_latents[:,:,:latent_window_size*connection_latent_sections,:,:]
        main_history_latents = real_history_latents[:,:,:latent_window_size*total_latent_sections,:,:]

        final_latents = torch.cat([connection_hisotry_latents[:,:,-latent_window_size:,:,:],
                                    main_history_latents,
                                    connection_hisotry_latents,
                                    main_history_latents[:,:,-latent_window_size:,:,:]],dim=2)

        transformer.to(offload_device)
        mm.soft_empty_cache()

        return {"samples": final_latents / vae_scaling_factor}, latent_window_size * 4 - 3, latent_window_size * 4

class SplitLoopFrames:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE",),
                 "start_frames": ("INT", {"default": 10, "min": 0, "step": 1, "tooltip": "Number of start frames to trim."}),
                 "end_frames": ("INT", {"default": 10, "min": 0, "step": 1, "tooltip": "Number of end frames to trim."}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "process"
    CATEGORY = "FramePackWrapper"

    def process(self, images: torch.Tensor, start_frames: int, end_frames: int):
        if start_frames > 0:
            images = images[start_frames:]
        if end_frames > 0:
            images = images[:-end_frames]
        return images,
NODE_CLASS_MAPPINGS = {
    "DownloadAndLoadFramePackModel": DownloadAndLoadFramePackModel,
    "FramePackSampler": FramePackSampler,
    "FramePackTorchCompileSettings": FramePackTorchCompileSettings,
    "FramePackFindNearestBucket": FramePackFindNearestBucket,
    "LoadFramePackModel": LoadFramePackModel,
    "FramePackLoraSelect": FramePackLoraSelect,
    "FramePackSingleFrameSampler": FramePackSingleFrameSampler,
    "FramePackLoopSampler": FramePackLoopSampler,
    "SplitLoopFrames": SplitLoopFrames,
    }
NODE_DISPLAY_NAME_MAPPINGS = {
    "DownloadAndLoadFramePackModel": "(Down)Load FramePackModel",
    "FramePackSampler": "FramePackSampler",
    "FramePackTorchCompileSettings": "Torch Compile Settings",
    "FramePackFindNearestBucket": "Find Nearest Bucket",
    "LoadFramePackModel": "Load FramePackModel",
    "FramePackLoraSelect": "Select Lora",
    "FramePackSingleFrameSampler": "Single Frame Sampler",
    "FramePackLoopSampler": "FramePackLoopSampler",
    "SplitLoopFrames": "SplitLoopFrames",
    }

