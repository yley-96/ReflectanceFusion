
import inspect
import argparse
import logging
import math
import os
import random
from pathlib import Path

import accelerate
import datasets
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import torchvision
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.state import AcceleratorState
from accelerate.utils import ProjectConfiguration, set_seed
from datasets import load_dataset
from huggingface_hub import create_repo, upload_folder
from packaging import version
from torchvision import transforms
from tqdm.auto import tqdm

import diffusers
from diffusers import AutoencoderKL
from diffusers.optimization import get_scheduler
from diffusers.training_utils import EMAModel
from diffusers.utils import check_min_version, deprecate, is_wandb_available
from diffusers.utils.import_utils import is_xformers_available
import torch.nn as nn

import lpips

if is_wandb_available():
    import wandb

logger = get_logger(__name__, log_level="INFO")


@torch.no_grad()
def log_validation(test_dataloader, vae, accelerator, weight_dtype, epoch):
    logger.info("Running validation... ")

    vae_model = accelerator.unwrap_model(vae)
    images = []
    for _, sample in enumerate(test_dataloader):
        x = sample["pixel_values"].to(weight_dtype)
        reconstructions = vae_model(x).sample
        images.append(
            torch.cat([sample["pixel_values"].cpu(), reconstructions.cpu()], axis=0)
        )

    for tracker in accelerator.trackers:
        if tracker.name == "tensorboard":
            np_images = np.stack([np.asarray(img) for img in images])
            tracker.writer.add_images(
                "Original (left) / Reconstruction (right)", np_images, epoch
            )
        elif tracker.name == "wandb":
            tracker.log(
                {
                    "Original (left) / Reconstruction (right)": [
                        wandb.Image(torchvision.utils.make_grid(image))
                        for _, image in enumerate(images)
                    ]
                }
            )
        else:
            logger.warn(f"image logging not implemented for {tracker.gen_images}")

    del vae_model
    torch.cuda.empty_cache()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Simple example of a VAE training script."
    )
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        default=None,
        required=True,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        required=False,
        help="Revision of pretrained model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help=(
            "Whether training should be resumed from a previous checkpoint. Use a path saved by"
            ' `--checkpointing_steps`, or `"latest"` to automatically select the last available checkpoint.'
        ),
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help=(
            "The name of the Dataset (from the HuggingFace hub) to train on (could be your own, possibly private,"
            " dataset). It can also be a path pointing to a local copy of a dataset in your filesystem,"
            " or to a folder containing files that 🤗 Datasets can understand."
        ),
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The config of the Dataset, leave as None if there's only one config.",
    )
    parser.add_argument(
        "--train_data_dir",
        type=str,
        default=None,
        help=(
            "A folder containing the training data. Folder contents must follow the structure described in"
            " https://huggingface.co/docs/datasets/image_dataset#imagefolder. In particular, a `metadata.jsonl` file"
            " must exist to provide the captions for the images. Ignored if `dataset_name` is specified."
        ),
    )
    parser.add_argument(
        "--image_column",
        type=str,
        default="image",
        help="The column of the dataset containing an image.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="vae-model-finetuned",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=None,
        help="The directory where the downloaded models and datasets will be stored.",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="A seed for reproducible training."
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=512,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--train_batch_size",
        type=int,
        default=16,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument("--num_train_epochs", type=int, default=100)
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        help="Whether or not to use gradient checkpointing to save memory at the expense of slower backward pass.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--scale_lr",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--onlytraindecoder",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--lpipslloss",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="constant",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
            ' "constant", "constant_with_warmup"]'
        ),
    )
    parser.add_argument(
        "--lr_warmup_steps",
        type=int,
        default=500,
        help="Number of steps for the warmup in the lr scheduler.",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
            " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
            " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
        ),
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`'
            ' (default), `"wandb"` and `"comet_ml"`. Use `"all"` to report to all integrations.'
        ),
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=int,
        default=500,
        help=(
            "Save a checkpoint of the training state every X updates. These checkpoints are only suitable for resuming"
            " training using `--resume_from_checkpoint`."
        ),
    )
    parser.add_argument(
        "--checkpoints_total_limit",
        type=int,
        default=None,
        help=(
            "Max number of checkpoints to store. Passed as `total_limit` to the `Accelerator` `ProjectConfiguration`."
            " See Accelerator::save_state https://huggingface.co/docs/accelerate/package_reference/accelerator#accelerate.Accelerator.save_state"
            " for more docs"
        ),
    )
    parser.add_argument(
        "--test_samples",
        type=int,
        default=4,
        help="Number of images to remove from training set to be used as validation.",
    )
    parser.add_argument(
        "--validation_epochs",
        type=int,
        default=5,
        help="Run validation every X epochs.",
    )
    parser.add_argument(
        "--tracker_project_name",
        type=str,
        default="vae-fine-tune",
        help=(
            "The `project_name` argument passed to Accelerator.init_trackers for"
            " more information see https://huggingface.co/docs/accelerate/v0.17.0/en/package_reference/accelerator#accelerate.Accelerator"
        ),
    )
    parser.add_argument(
        "--kl_scale",
        type=float,
        # default=1,
        default=1e-6,
        help="Scaling factor for the Kullback-Leibler divergence penalty term.",
    )
    parser.add_argument(
        "--lpips_scale",
        type=float,
        default=1e-1,
        help="Scaling factor for the LPIPS metric",
    )
    # parser.add_argument(
    #     "--max_train_steps",
    #     type=int,
    #     default=392501,
    #     help="Scaling factor for the LPIPS metric",
    # )

    args = parser.parse_args()
    # Sanity checks
    if args.dataset_name is None and args.train_data_dir is None:
        raise ValueError("Need either a dataset name or a training folder.")

    return args


def main():
    args = parse_args()

    logging_dir = os.path.join(args.output_dir, args.logging_dir)

    accelerator_project_config = ProjectConfiguration(
        total_limit=args.checkpoints_total_limit,
        project_dir=args.output_dir,
        logging_dir=logging_dir,
    )

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        project_config=accelerator_project_config,
    )

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)

    if args.seed is not None:
        set_seed(args.seed)

    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    # Load vae
    vae = AutoencoderKL.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="vae", revision=args.revision
    )
    new_output_channels = 12  
    from copy import deepcopy

    copied_mid_block1 = deepcopy(vae.decoder.mid_block)
    copied_mid_block2 = deepcopy(vae.decoder.mid_block)
    copied_mid_block3 = deepcopy(vae.decoder.mid_block)
    copied_mid_block4 = deepcopy(vae.decoder.mid_block)
    vae.decoder.copied_mid_block1 = copied_mid_block1
    vae.decoder.copied_mid_block2 = copied_mid_block2
    vae.decoder.copied_mid_block3 = copied_mid_block3
    vae.decoder.copied_mid_block4 = copied_mid_block4
    # adjust_conv_layer2 = nn.Conv2d(32, 128, kernel_size=1, stride=1, padding=0)
    

    # vae.decoder.adjust_conv_layer2 = adjust_conv_layer2


    # adjust_conv_layer = nn.Conv2d(128, 32, kernel_size=1, stride=1, padding=0)
    # vae.decoder.adjust_conv_layer = adjust_conv_layer
 
    numc=128

    # for attn in vae.decoder.copied_mid_block4.attentions:
    #     attn.to_q = nn.Linear(numc, numc, bias=True)
    #     attn.to_k = nn.Linear(numc, numc, bias=True)
    #     attn.to_v = nn.Linear(numc, numc, bias=True)
    #     attn.to_out[0] = nn.Linear(numc, numc, bias=True)
   


    for resnet_block in vae.decoder.copied_mid_block3.resnets:
        resnet_block.norm1 = nn.GroupNorm(32, numc, eps=1e-06, affine=True)
        resnet_block.conv1 = nn.Conv2d(numc, numc, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
        resnet_block.norm2 = nn.GroupNorm(32, numc, eps=1e-06, affine=True)
        resnet_block.conv2 = nn.Conv2d(numc, numc, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
    for resnet_block in vae.decoder.copied_mid_block4.resnets:
        resnet_block.norm1 = nn.GroupNorm(32, numc, eps=1e-06, affine=True)
        resnet_block.conv1 = nn.Conv2d(numc, numc, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
        resnet_block.norm2 = nn.GroupNorm(32, numc, eps=1e-06, affine=True)
        resnet_block.conv2 = nn.Conv2d(numc, numc, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))


    copied_mid_block3.attentions = nn.ModuleList()
    copied_mid_block4.attentions = nn.ModuleList()
    # for attention_module in vae.decoder.copied_mid_block4.attentions:
    #     attention_module.group_norm = nn.GroupNorm(32, numc, eps=1e-06, affine=True)
    
    # vae.decoder.adjust_conv_layer = adjust_conv_layer



    def new_forward(
        self,
        sample: torch.FloatTensor,
        latent_embeds= None,
    ):
        """The forward method of the `Decoder` class."""

        sample = self.conv_in(sample)

        upscale_dtype = next(iter(self.up_blocks.parameters())).dtype
        if self.training and self.gradient_checkpointing:

            def create_custom_forward(module):
                def custom_forward(*inputs):
                    return module(*inputs)

                return custom_forward

            # if is_torch_version(">=", "1.11.0"):
            if True:
                # middle
                sample = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(self.mid_block),
                    sample,
                    latent_embeds,
                    use_reentrant=False,
                )
                sample = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(self.copied_mid_block1),
                    sample,
                    latent_embeds,
                    use_reentrant=False,
                )
                sample = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(self.copied_mid_block2),
                    sample,
                    latent_embeds,
                    use_reentrant=False,
                )
                sample = sample.to(upscale_dtype)

                # up
                for up_block in self.up_blocks:
                    sample = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(up_block),
                        sample,
                        latent_embeds,
                        use_reentrant=False,
                    )
                    
                # sample=adjust_conv_layer(sample)

                sample = self.copied_mid_block3(sample, latent_embeds)

                sample = self.copied_mid_block4(sample, latent_embeds)

                sample = sample.to(upscale_dtype)
                # sample=adjust_conv_layer2(sample)
            else:
                # middle
                sample = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(self.mid_block), sample, latent_embeds
                )
                sample = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(self.copied_mid_block1), sample, latent_embeds
                )
                sample = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(self.copied_mid_block2), sample, latent_embeds
                )
                sample = sample.to(upscale_dtype)

                # up
                for up_block in self.up_blocks:
                    sample = torch.utils.checkpoint.checkpoint(create_custom_forward(up_block), sample, latent_embeds)
        else:
            # middle
            sample = self.mid_block(sample, latent_embeds)
            sample = self.copied_mid_block1(sample, latent_embeds)
            sample = self.copied_mid_block2(sample, latent_embeds)
            sample = sample.to(upscale_dtype)

            # up
            for up_block in self.up_blocks:
                sample = up_block(sample, latent_embeds)


            # sample=adjust_conv_layer(sample)
            sample = self.copied_mid_block3(sample, latent_embeds)
            sample = self.copied_mid_block4(sample, latent_embeds)
            sample = sample.to(upscale_dtype)
            # sample=adjust_conv_layer2(sample)



        # post-process
        if latent_embeds is None:
            sample = self.conv_norm_out(sample)
        else:
            sample = self.conv_norm_out(sample, latent_embeds)
        sample = self.conv_act(sample)
        sample = self.conv_out(sample)

        return sample
    vae.decoder.forward = new_forward.__get__(vae.decoder)


    vae.decoder.conv_out = nn.Conv2d(
        in_channels=vae.decoder.conv_out.in_channels, 
        out_channels=new_output_channels, 
        kernel_size=vae.decoder.conv_out.kernel_size, 
        stride=vae.decoder.conv_out.stride, 
        padding=vae.decoder.conv_out.padding
    )
    vae.requires_grad_(True)
    vae_params = vae.parameters()
    if args.onlytraindecoder==True:
        for param in vae.encoder.parameters():
            param.requires_grad = False

    print(vae)
    print(inspect.getsource(vae.decoder.mid_block.forward))


    if args.gradient_checkpointing:
        vae.enable_gradient_checkpointing()

    if args.scale_lr:
        args.learning_rate = (
            args.learning_rate
            * args.gradient_accumulation_steps
            * args.train_batch_size
            * accelerator.num_processes
        )

    optimizer = torch.optim.AdamW(vae_params, lr=args.learning_rate)

    # Get the datasets: you can either provide your own training and evaluation files (see below)
    # or specify a Dataset from the hub (the dataset will be downloaded automatically from the datasets Hub).

    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    if args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        dataset = load_dataset(
            args.dataset_name,
            args.dataset_config_name,
            cache_dir=args.cache_dir,
        )
    else:
        data_files = {}
        if args.train_data_dir is not None:
            data_files["train"] = os.path.join(args.train_data_dir, "**")
        dataset = load_dataset(
            "imagefolder",
            data_files=data_files,
            cache_dir=args.cache_dir,
        )

    column_names = dataset["train"].column_names
    if args.image_column is None:
        image_column = column_names[0]
    else:
        image_column = args.image_column
        if image_column not in column_names:
            raise ValueError(
                f"--image_column' value '{args.image_column}' needs to be one of: {', '.join(column_names)}"
            )

    train_transforms = transforms.Compose(
        [
            transforms.Resize(
                (512, 2560), interpolation=transforms.InterpolationMode.BILINEAR
            ),
            # transforms.RandomCrop(args.resolution),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )

    def preprocess(examples):
        images = [image.convert("RGB") for image in examples[image_column]]
        examples["pixel_values"] = [train_transforms(image) for image in images]
        return examples

    with accelerator.main_process_first():
        # Split into train/test
        dataset = dataset["train"].train_test_split(test_size=args.test_samples)
        # Set the training transforms
        train_dataset = dataset["train"].with_transform(preprocess)
        test_dataset = dataset["test"].with_transform(preprocess)

    def collate_fn(examples):
        pixel_values = torch.stack([example["pixel_values"] for example in examples])
        pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()
        return {"pixel_values": pixel_values}

    # DataLoaders creation:
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=collate_fn,
        batch_size=args.train_batch_size,
    )

    test_dataloader = torch.utils.data.DataLoader(
        test_dataset, shuffle=True, collate_fn=collate_fn
    )

    # for step, batch in enumerate(train_dataloader):
    #     datafile = batch["pixel_values"].to(torch.float32)
    #     print("shape",datafile.shape)
    #     input_image = datafile[:, :, :288]  # Shape will be [3, 288, 288]
    #     print("shape",input_image.shape)
    #     stacked_image = datafile[:, :, 288:]  # Shape will be [3, 288, 1152]
    #     target = stacked_image.view(12, 288, 288) 

    #     print("shape",target.shape)










    

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.num_train_epochs * args.gradient_accumulation_steps,
        # num_training_steps=392501,

    )

    # Prepare everything with our `accelerator`.
    (
        vae,
        optimizer,
        train_dataloader,
        test_dataloader,
        lr_scheduler,
    ) = accelerator.prepare(
        vae, optimizer, train_dataloader, test_dataloader, lr_scheduler
    )

    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    vae.to(accelerator.device, dtype=weight_dtype)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        tracker_config = dict(vars(args))
        accelerator.init_trackers(args.tracker_project_name, tracker_config)

    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps
    )
    args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch

    # ------------------------------ TRAIN ------------------------------ #
    total_batch_size = (
        args.train_batch_size
        * accelerator.num_processes
        * args.gradient_accumulation_steps
    )

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num test samples = {len(test_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    logger.info(
        f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}"
    )
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    global_step = 0
    first_epoch = 0
# Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint != "latest":
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(args.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{args.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            args.resume_from_checkpoint = None
            initial_global_step = 0
        else:
            accelerator.print(f"Resuming from checkpoint {path}")
            accelerator.load_state(os.path.join(args.output_dir, path))
            global_step = int(path.split("-")[1])

            initial_global_step = global_step
            first_epoch = global_step // num_update_steps_per_epoch

    else:
        initial_global_step = 0

    progress_bar = tqdm(
        range(global_step, args.max_train_steps),
        initial=initial_global_step,
        disable=not accelerator.is_local_main_process,
    )
    progress_bar.set_description("Steps")

    lpips_loss_fn = lpips.LPIPS(net="alex").to(accelerator.device)

    for epoch in range(first_epoch, args.num_train_epochs):
        vae.train()
        train_loss = 0.0
        for step, batch in enumerate(train_dataloader):
            with accelerator.accumulate(vae):
                datafile = batch["pixel_values"].to(weight_dtype)
                print("shape",datafile.shape)
                input_image = datafile[:,:, :, :512]  # Shape will be [3, 288, 288]
                # stacked_image = datafile[:,:, 512:, :]  # Shape will be [3, 288, 1152]
                # print("shape",stacked_image.shape)
                # target = stacked_image.view(12, 512, 512)

           


                images = torch.chunk(datafile, 5, dim=3)  


                target = torch.cat([img.squeeze(0) for img in images[1:]], dim=0)  #  [12, 512, 512]
                target = target.unsqueeze(0)


                print("target shape",target.shape)

                posterior = vae.module.encode(input_image).latent_dist
                z = posterior.mode()
                pred = vae.module.decode(z).sample
                print("pred shape",pred.shape)

                kl_loss = posterior.kl().mean()
                mse_loss = F.mse_loss(pred, target, reduction="mean")
                total_lpips_loss = 0.0
                for i in range(4):

                    start_channel = i * 3
                    end_channel = (i + 1) * 3


                    pred_channel = pred[:, start_channel:end_channel]
                    target_channel = target[:, start_channel:end_channel]


                    loss = lpips_loss_fn(pred_channel, target_channel).mean()
                    total_lpips_loss += loss
                lpips_loss = total_lpips_loss/4

                loss = (
                    mse_loss + args.lpips_scale * lpips_loss + args.kl_scale * kl_loss
                )

                # Gather the losses across all processes for logging (if we use distributed training).
                avg_loss = accelerator.gather(loss.repeat(args.train_batch_size)).mean()
                train_loss += avg_loss.item() / args.gradient_accumulation_steps

                accelerator.backward(loss)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1
                accelerator.log({"train_loss": train_loss}, step=global_step)
                train_loss = 0.0

                if global_step % args.checkpointing_steps == 0:
                    if accelerator.is_main_process:
                        save_path = os.path.join(
                            args.output_dir, f"checkpoint-{global_step}"
                        )
                        accelerator.save_state(save_path)
                        logger.info(f"Saved state to {save_path}")

            logs = {
                "step_loss": loss.detach().item(),
                "lr": lr_scheduler.get_last_lr()[0],
                "mse": mse_loss.detach().item(),
                "lpips": lpips_loss.detach().item(),
                "kl": kl_loss.detach().item(),
            }
            accelerator.log(logs)
            progress_bar.set_postfix(**logs)

        # if accelerator.is_main_process:
        #     if epoch % args.validation_epochs == 0:
        #         with torch.no_grad():
        #             log_validation(test_dataloader, vae, accelerator, weight_dtype, epoch)

    # Create the pipeline using the trained modules and save it.
    accelerator.wait_for_everyone()
    if accelerator.is_main_process:
        vae = accelerator.unwrap_model(vae)
        vae.save_pretrained(args.output_dir)

    accelerator.end_training()


if __name__ == "__main__":
    main()
