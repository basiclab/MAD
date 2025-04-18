import argparse
import datetime
import gc
import os
import os.path as osp
import time

import accelerate
import torch
import torch.optim as optim
import torchvision.transforms as T
from accelerate.utils import GradientAccumulationPlugin
from diffusers.optimization import get_constant_schedule_with_warmup
from diffusers.schedulers import DDIMScheduler, DDPMScheduler
from diffusers.training_utils import EMAModel
from diffusers.utils import make_image_grid, numpy_to_pil
from loguru import logger
from tqdm import tqdm
from transformers import CLIPTokenizer

from config import create_cfg, merge_possible_with_base, show_config
from dataset import get_makeup_loader
from misc import AverageMeter, MetricMeter
from modeling import build_model

SCHEDULER_FUNC = {
    "ddim": DDIMScheduler,
    "ddpm": DDPMScheduler,
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None, type=str)
    parser.add_argument("--generate-only", action="store_true", default=False)
    parser.add_argument("--save-file-name", default="generated.png", type=str)
    parser.add_argument("--opts", nargs=argparse.REMAINDER, default=None, type=str)
    return parser.parse_args()


@torch.inference_mode()
def evaluate(cfg, unet, noise_scheduler, device, filename, accelerator):
    unet.eval()
    num_images = cfg.EVAL.BATCH_SIZE
    image_shape = (
        num_images,
        cfg.MODEL.IN_CHANNELS,
        cfg.TRAIN.IMAGE_SIZE,
        cfg.TRAIN.IMAGE_SIZE,
    )
    images = torch.randn(image_shape, device=device)

    tokenizer = CLIPTokenizer.from_pretrained(
        "CompVis/stable-diffusion-v1-4", subfolder="tokenizer"
    )
    text_inputs = tokenizer(
        "",
        padding="max_length",
        max_length=tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_input_ids = text_inputs.input_ids
    text_input_ids = text_input_ids.repeat(num_images, 1, 1).to(device)
    if cfg.MODEL.LABEL_DIM > 0:
        labels = torch.Tensor([0] * (num_images // 2) + [1] * (num_images // 2)).long().to(device)
        labels = (
            torch.nn.functional.one_hot(labels[:num_images], cfg.MODEL.LABEL_DIM)
            .squeeze()
            .float()
            .to(device)
        )
    else:
        labels = None

    noise_scheduler.set_timesteps(cfg.EVAL.SAMPLE_STEPS, device=device)
    for t in tqdm(noise_scheduler.timesteps, disable=not accelerator.is_main_process):
        model_output = unet(images, t.reshape(-1), text_input_ids, class_labels=labels).float()
        if cfg.EVAL.SCHEDULER == "ddim":
            images = noise_scheduler.step(
                model_output, t, images, use_clipped_model_output=True, eta=cfg.EVAL.ETA
            ).prev_sample
        else:
            images = noise_scheduler.step(model_output, t, images).prev_sample

    images = accelerator.gather([images])
    if accelerator.is_main_process:
        images = torch.cat(images, dim=0)
        images = (images.to(torch.float32).clamp(-1, 1) + 1) / 2
        images = numpy_to_pil(images.cpu().permute(0, 2, 3, 1).numpy())

        make_image_grid(images, cols=4, rows=len(images) // 4).save(filename)
        logger.info(f"Save generated samples to {filename}...")


def main(args):
    cfg = create_cfg()

    if args.config is not None:
        merge_possible_with_base(cfg, args.config)
    if args.opts is not None:
        cfg.merge_from_list(args.opts)

    configuration = accelerate.utils.ProjectConfiguration(
        project_dir=cfg.PROJECT_DIR,
    )
    plugin = GradientAccumulationPlugin(
        sync_with_dataloader=False, num_steps=cfg.TRAIN.GRADIENT_ACCUMULATION_STEPS
    )
    kwargs = accelerate.InitProcessGroupKwargs(timeout=datetime.timedelta(seconds=3600))
    accelerator = accelerate.Accelerator(
        kwargs_handlers=[kwargs],
        gradient_accumulation_plugin=plugin,
        log_with=["aim"],
        project_config=configuration,
        mixed_precision=cfg.TRAIN.MIXED_PRECISION,
    )

    accelerator.init_trackers(project_name=cfg.PROJECT_NAME)
    if accelerator.is_main_process:
        show_config(cfg)

    device = accelerator.device
    if accelerator.is_main_process:
        os.makedirs(osp.join(cfg.PROJECT_DIR, "checkpoints"), exist_ok=True)
        os.makedirs(osp.join(cfg.PROJECT_DIR, "generate"), exist_ok=True)

    # Build model
    model = build_model(cfg)

    noise_scheduler = SCHEDULER_FUNC[cfg.EVAL.SCHEDULER](
        num_train_timesteps=cfg.TRAIN.SAMPLE_STEPS,
        prediction_type=cfg.TRAIN.NOISE_SCHEDULER.PRED_TYPE,
        beta_schedule=cfg.TRAIN.NOISE_SCHEDULER.TYPE,
        # For linear only
        beta_start=cfg.TRAIN.NOISE_SCHEDULER.BETA_START,
        beta_end=cfg.TRAIN.NOISE_SCHEDULER.BETA_END,
    )

    if (cfg.TRAIN.RESUME is None) and (cfg.MODEL.PRETRAINED is not None):
        if accelerator.is_main_process:
            logger.info(f"Load pretrained model from {cfg.MODEL.PRETRAINED}...")

        with accelerator.main_process_first():
            weight = torch.load(cfg.MODEL.PRETRAINED, map_location="cpu")
        load_res = model.load_state_dict(weight["model"], strict=False)
        if accelerator.is_main_process:
            logger.info(f"Load result for model: {load_res}")
        del weight
        torch.cuda.empty_cache()
        gc.collect()

    ema_model = EMAModel(
        [param for param in model.parameters() if param.requires_grad],
        decay=cfg.TRAIN.EMA_MAX_DECAY,
        use_ema_warmup=True,
        inv_gamma=cfg.TRAIN.EMA_INV_GAMMA,
        power=cfg.TRAIN.EMA_POWER,
    )

    # Build data loader
    transforms = T.Compose(
        [
            T.Resize((cfg.TRAIN.IMAGE_SIZE, cfg.TRAIN.IMAGE_SIZE)),
            T.RandomHorizontalFlip(),
            T.ToTensor(),
            T.Normalize([0.5], [0.5]),
        ]
    )
    dataloader = get_makeup_loader(cfg, train=True, transforms=transforms)

    # Build optimizer
    optimizer = optim.AdamW(
        [param for param in model.parameters() if param.requires_grad],
        lr=cfg.TRAIN.LR,
        betas=(0.95, 0.999),
        eps=1e-7,
    )
    lr_scheduler = get_constant_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=cfg.TRAIN.LR_WARMUP,
    )

    start_iter = 0
    if cfg.TRAIN.RESUME is not None:
        assert osp.exists(cfg.TRAIN.RESUME), "Resume file not found."
        with accelerator.main_process_first():
            state_dict = torch.load(cfg.TRAIN.RESUME, map_location="cpu")
            ema_model.load_state_dict(state_dict["ema_state_dict"])
            if not args.generate_only:
                model.load_state_dict(state_dict["state_dict"])
                optimizer.load_state_dict(state_dict["optimizer"])
                lr_scheduler.load_state_dict(state_dict["lr_scheduler"])
                start_iter = state_dict["iter"] + 1
        if accelerator.is_main_process:
            logger.info(f"Resume checkpoint from {cfg.TRAIN.RESUME}...")
        del state_dict
        torch.cuda.empty_cache()
        gc.collect()

    model, optimizer, lr_scheduler, dataloader = accelerator.prepare(
        model, optimizer, lr_scheduler, dataloader
    )
    ema_model.to(accelerator.device)

    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16

    if args.generate_only:
        unet = accelerator.unwrap_model(model)
        ema_model.copy_to([param for param in unet.parameters() if param.requires_grad])
        evaluate(cfg, unet, noise_scheduler, device, args.save_file_name, accelerator)
        return

    loss_meter = MetricMeter()
    iter_time = AverageMeter()

    max_iter = cfg.TRAIN.MAX_ITER
    # prefetcher = DataPrefetcher(dataloader, device=device)
    loader = iter(dataloader)

    for cur_iter in range(start_iter, max_iter):
        end = time.time()
        model.train()
        try:
            data = next(loader)
        except StopIteration:
            loader = iter(dataloader)
            data = next(loader)
        img = data["image"]
        label = data["label"] if data["label"] is not None else None
        text = data["text"]

        t = torch.randint(0, cfg.TRAIN.TIME_STEPS, (img.shape[0],), device=device).long()
        noise = torch.randn_like(img, dtype=weight_dtype)
        noise_data = noise_scheduler.add_noise(img, noise, t)

        with accelerator.accumulate(model):
            pred = model(noise_data, t, text=text, class_labels=label)

            if cfg.TRAIN.NOISE_SCHEDULER.PRED_TYPE == "epsilon":
                loss = torch.nn.functional.mse_loss(pred.float(), noise.float())
            else:
                raise ValueError("Not supported prediction type.")

            accelerator.backward(loss)
            if accelerator.sync_gradients:
                for param in model.parameters():
                    if param.grad is not None:
                        torch.nan_to_num(param.grad, nan=0, posinf=1e5, neginf=-1e5, out=param.grad)
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

        if accelerator.sync_gradients:
            ema_model.step(model.parameters())

        iter_time.update(time.time() - end)
        loss_meter.update({"loss": loss.item()})

        if (cur_iter + 1) % cfg.TRAIN.LOG_INTERVAL == 0 and accelerator.is_main_process:
            nb_future_iters = max_iter - (cur_iter + 1)
            eta_seconds = iter_time.avg * nb_future_iters
            eta_str = str(datetime.timedelta(seconds=int(eta_seconds)))
            logger.info(
                f"iter: [{cur_iter + 1}/{max_iter}]\t"
                f"time: {iter_time.val:.3f} ({iter_time.avg:.3f})\t"
                f"eta: {eta_str}\t"
                f"lr: {optimizer.param_groups[-1]['lr']:.2e}\t"
                f"{loss_meter}"
            )
            accelerator.log(loss_meter.get_log_dict(), step=cur_iter + 1)

        if (
            ((cur_iter + 1) % cfg.TRAIN.SAVE_INTERVAL == 0) or (cur_iter == max_iter - 1)
        ) and accelerator.is_main_process:
            state_dict = {
                "state_dict": accelerator.unwrap_model(model).state_dict(),
                "optimizer": optimizer.optimizer.state_dict(),
                "lr_scheduler": lr_scheduler.scheduler.state_dict(),
                "iter": cur_iter,
                "ema_state_dict": ema_model.state_dict(),
            }
            save_name = (
                f"checkpoint_{cur_iter + 1}.pth" if cur_iter != max_iter - 1 else "final.pth"
            )
            torch.save(state_dict, osp.join(cfg.PROJECT_DIR, "checkpoints", save_name))
            logger.info(f"Save checkpoint to {save_name}...")

        if ((cur_iter + 1) % cfg.TRAIN.SAMPLE_INTERVAL == 0) or (cur_iter == max_iter - 1):
            filename = osp.join(cfg.PROJECT_DIR, "generate", f"iter_{cur_iter+1:03d}.png")
            unet = accelerator.unwrap_model(model)
            ema_model.store(unet.parameters())
            ema_model.copy_to([param for param in unet.parameters() if param.requires_grad])
            evaluate(cfg, unet, noise_scheduler, device, filename, accelerator)
            ema_model.restore(unet.parameters())
        accelerator.wait_for_everyone()

    accelerator.end_training()


if __name__ == "__main__":
    args = parse_args()
    main(args)
