import os
import random

import lightning as L
import matplotlib.pyplot as plt
import numpy as np
import segmentation_models_pytorch as smp
import torch
import torchvision.transforms.functional as TF
from lightning.pytorch.callbacks import Callback, ModelCheckpoint, TQDMProgressBar
from lightning.pytorch.loggers import TensorBoardLogger
from PIL import Image
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms


class DensityBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.relu(self.bn1(self.conv1(x)))
        x = self.relu(self.bn2(self.conv2(x)))
        return x


class DensityLoss(nn.Module):
    def __init__(self, lambda_mse=1.0, lambda_mape=0.5):
        super().__init__()
        self.lambda_mse = lambda_mse
        self.lambda_mape = lambda_mape

    def forward(self, pred, target):
        # Calculate the loss
        mse_loss = F.mse_loss(pred, target)
        pred_count = pred.sum(dim=(2, 3)) + 1
        target_count = target.sum(dim=(2, 3)) + 1
        mape_loss = torch.mean(torch.abs(target_count - pred_count) / target_count)
        return self.lambda_mse * mse_loss + self.lambda_mape * mape_loss


class UNetLightningModule(L.LightningModule):
    def __init__(
        self, in_channels, out_channels, decoder_channels, learning_rate, loss_fn="mse"
    ):
        super().__init__()
        self.learning_rate = learning_rate
        model = smp.Unet(
            encoder_name="efficientnet-b1",  # choose encoder, e.g. mobilenet_v2 or efficientnet-b7
            encoder_weights="imagenet",  # use `imagenet` pre-trained weights for encoder initialization
            decoder_channels=decoder_channels,  # input channels param for convolutions in decoder
            in_channels=in_channels,  # model input channels (1 for grayscale images, 3 for RGB, etc.)
            classes=out_channels,  # model output channels (number of classes)
            decoder_attention_type="scse",
        )
        self.encoder = model.encoder
        self.decoder = model.decoder
        self.density_block = DensityBlock(32, 64)
        self.final_conv = nn.Sequential(
            nn.Conv2d(64, out_channels, kernel_size=1),
            nn.ReLU(),
        )
        self.density_loss = DensityLoss()
        self.loss_fn = loss_fn

    def forward(self, x):
        features = self.encoder(x)
        decoder_output = self.decoder(*features)
        density = self.density_block(decoder_output)
        return self.final_conv(density)
        # return self.model(x)

    def training_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)

        if self.loss_fn == "mse":
            loss = F.mse_loss(y_hat, y)
        else:
            loss = self.density_loss(y_hat, y)
        # print(f"Loss: {loss.item()}")
        self.log("train_loss", loss, prog_bar=True, on_epoch=True)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)

        # Calculate various metrics

        # Calculate count error
        true_count = y.sum(dim=(1, 2, 3)) / 100
        pred_count = y_hat.sum(dim=(1, 2, 3)) / 100
        count_error = torch.abs(true_count - pred_count)

        # Log all metrics
        self.log(
            "val_count_error",
            count_error.mean(),
            prog_bar=True,
            on_step=False,
            on_epoch=True,
        )
        if self.loss_fn == "mse":
            loss = F.mse_loss(y_hat, y)
        else:
            loss = self.density_loss(y_hat, y)

        # print(f"Validation Loss: {loss.item()}")
        self.log("val_mse_loss", loss, prog_bar=True, on_epoch=True)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        return {
            "optimizer": optimizer,
            # 'gradient_clip_val': 0.1,
            # 'gradient_clip_algorithm': 'norm'
        }


class JointTransform:
    def __init__(self, transform):
        self.transform = transform

    def __call__(self, image, density_map):
        if isinstance(self.transform, transforms.RandomCrop):
            # Get desired crop size
            crop_height, crop_width = self.transform.size
            img_width, img_height = image.size

            # Check if padding is needed
            pad_height = max(crop_height - img_height, 0)
            pad_width = max(crop_width - img_width, 0)

            if pad_height > 0 or pad_width > 0:
                # Calculate padding
                padding = [
                    pad_width // 2,
                    pad_height // 2,
                    pad_width - (pad_width // 2),
                    pad_height - (pad_height // 2),
                ]

                # Apply padding
                image = TF.pad(image, padding, fill=0)
                density_map = TF.pad(density_map, padding, fill=0)

            # Now apply random crop
            i, j, h, w = self.transform.get_params(image, self.transform.size)
            image = TF.crop(image, i, j, h, w)
            density_map = TF.crop(density_map, i, j, h, w)
        else:
            seed = torch.randint(0, 2**32, (1,)).item()
            torch.manual_seed(seed)
            image = self.transform(image)
            torch.manual_seed(seed)
            density_map = self.transform(density_map)
        return image, density_map


class CustomHSVTransform:
    def __init__(self, hsv_h, hsv_s, hsv_v):
        self.hsv_h = hsv_h
        self.hsv_s = hsv_s
        self.hsv_v = hsv_v

    def __call__(self, img):
        # Convert image to HSV
        img = TF.to_pil_image(img)
        if random.random() < 0.5:
            # Generate random adjustments within the specified limits
            hue_adjustment = random.uniform(-self.hsv_h, self.hsv_h)
            saturation_adjustment = random.uniform(1 - self.hsv_s, 1 + self.hsv_s)
            value_adjustment = random.uniform(1 - self.hsv_v, 1 + self.hsv_v)

            # Apply the random adjustments
            img = TF.adjust_hue(img, hue_adjustment)
            img = TF.adjust_saturation(img, saturation_adjustment)
            img = TF.adjust_brightness(img, value_adjustment)
        return TF.to_tensor(img)


class CornKernelDataset(Dataset):
    def __init__(self, image_dir, density_map_dir, transform=None):
        self.image_dir = image_dir
        self.density_map_dir = density_map_dir
        self.transform = transform
        self.image_files = [
            f.split(".")[0] for f in os.listdir(image_dir) if f.endswith(".jpg")
        ]

    def __len__(self):
        return len(self.image_files)

    def __getitem__(self, idx):
        img_name = self.image_files[idx]

        image_path = os.path.join(self.image_dir, img_name + ".jpg")
        density_map_path = os.path.join(
            self.density_map_dir, f"{img_name}_class_0_density.npy"
        )

        # Load image
        image = Image.open(image_path).convert("RGB")

        # Load density map
        density_map = np.load(density_map_path)
        density_map = torch.from_numpy(density_map).float().unsqueeze(0)

        if self.transform:
            for t in self.transform.transforms:
                if isinstance(t, JointTransform):
                    # print(f"Joint Transform {img_name}")
                    image, density_map = t(image, density_map)
                else:
                    image = t(image)

        return image, density_map


class CornKernelDataModule(L.LightningDataModule):
    def __init__(
        self,
        batch_size,
        num_workers,
        train_image_dir,
        train_density_map_dir,
        val_image_dir,
        val_density_map_dir,
    ):
        super().__init__()
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.train_image_dir = train_image_dir
        self.train_density_map_dir = train_density_map_dir
        self.val_image_dir = val_image_dir
        self.val_density_map_dir = val_density_map_dir

        self.transform = transforms.Compose(
            [
                JointTransform(transforms.RandomHorizontalFlip(p=0.5)),
                JointTransform(transforms.RandomVerticalFlip(p=0.5)),
                JointTransform(transforms.RandomCrop((480, 640))),
                # JointTransform(transforms.RandomRotation(degrees=180)),
                transforms.ToTensor(),  # This will only be applied to the image
                # JointTransform(transforms.RandomErasing(p=0.1)),
                # CustomHSVTransform(hsv_h=0.1, hsv_s=0.1, hsv_v=0.2),
            ]
        )

        self.train_transform = self.transform
        self.val_transform = self.transform

    def setup(self, stage=None):
        self.train_dataset = CornKernelDataset(
            image_dir=self.train_image_dir,
            density_map_dir=self.train_density_map_dir,
            transform=self.train_transform,
        )

        self.val_dataset = CornKernelDataset(
            image_dir=self.val_image_dir,
            density_map_dir=self.val_density_map_dir,
            transform=self.val_transform,
        )

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset, batch_size=self.batch_size, num_workers=self.num_workers
        )

    def predict_dataloader(self) -> torch.Any:
        return super().predict_dataloader()


class DensityMapVisualizationCallback(Callback):
    def __init__(self, val_samples, cmap, vmin, vmax, num_samples=4):
        super().__init__()
        self.val_imgs, self.val_density_maps = val_samples
        self.num_samples = num_samples
        self.cmap = cmap
        self.vmin = vmin
        self.vmax = vmax
        # randomly sample 4 indices from val_imgs
        self.idxs = random.sample(range(len(self.val_imgs)), self.num_samples)

    def on_validation_epoch_end(self, trainer, pl_module):
        # Move validation samples to the same device as the model
        val_imgs = self.val_imgs[self.idxs].to(pl_module.device)
        val_density_maps = self.val_density_maps[self.idxs].to(pl_module.device)
        # val_imgs = self.val_imgs[: self.num_samples].to(pl_module.device)
        # val_density_maps = self.val_density_maps[: self.num_samples].to(
        #     pl_module.device
        # )

        # Get predictions
        pl_module.eval()
        with torch.no_grad():
            preds = pl_module(val_imgs)
        pl_module.train()

        # print(f"Validation Predictions Shape: {preds.shape}")
        # print(f"Ground truth map shape {val_density_maps.shape} ")

        # Create a figure to display images, ground truth, and predictions
        fig, axes = plt.subplots(
            self.num_samples, 3, figsize=(15, 5 * self.num_samples)
        )
        for i in range(self.num_samples):
            # Display input image
            axes[i, 0].imshow(val_imgs[i].cpu().permute(1, 2, 0))
            axes[i, 0].set_title("Input Image")
            axes[i, 0].axis("off")

            # Display ground truth density map
            axes[i, 1].imshow(
                val_density_maps[i].cpu().squeeze(),
                cmap=self.cmap,
                vmin=self.vmin,
                vmax=self.vmax,
            )
            axes[i, 1].set_title("Ground Truth")
            axes[i, 1].axis("off")

            # Display predicted density map
            axes[i, 2].imshow(
                preds[i].cpu().squeeze(), cmap=self.cmap, vmin=self.vmin, vmax=self.vmax
            )
            axes[i, 2].set_title("Prediction")
            axes[i, 2].axis("off")

        plt.tight_layout()

        # Log the figure to TensorBoard
        trainer.logger.experiment.add_figure(
            "Validation Predictions", fig, trainer.global_step
        )
        plt.close(fig)


if __name__ == "__main__":
    root_dir = "../"
    # Define hyperparameters
    hparams = {
        # Model hyperparameters
        "in_channels": 3,
        "out_channels": 1,
        "decoder_channels": (512, 256, 128, 64, 32),
        "learning_rate": 1e-4,
        "loss_fn": "mse",  # "mse" or "custom"
        # Data hyperparameters
        "batch_size": 8,
        "num_workers": 1,
        # Training hyperparameters
        "max_epochs": 300,
        # Paths
        "train_image_dir": f"{root_dir}datasets/corn_kernel_density/train/original_size_dmx100/sigma-12",
        "train_density_map_dir": f"{root_dir}datasets/corn_kernel_density/train/original_size_dmx100/sigma-12",
        "val_image_dir": f"{root_dir}datasets/corn_kernel_density/val/original_size_dmx100/sigma-12",
        "val_density_map_dir": f"{root_dir}datasets/corn_kernel_density/val/original_size_dmx100/sigma-12",
    }

    # Create model

    model = UNetLightningModule(
        in_channels=hparams["in_channels"],
        out_channels=hparams["out_channels"],
        decoder_channels=hparams["decoder_channels"],
        learning_rate=hparams["learning_rate"],
    )

    # Create data module
    data_module = CornKernelDataModule(
        batch_size=hparams["batch_size"],
        num_workers=hparams["num_workers"],
        train_image_dir=hparams["train_image_dir"],
        train_density_map_dir=hparams["train_density_map_dir"],
        val_image_dir=hparams["val_image_dir"],
        val_density_map_dir=hparams["val_density_map_dir"],
    )

    # Set up the data module
    data_module.setup()

    val_dataloader = data_module.val_dataloader()
    val_samples = next(iter(val_dataloader))

    # train_dataloader = data_module.train_dataloader()
    # train_samples = next(iter(train_dataloader))

    # Create visualization callback
    visualization_callback = DensityMapVisualizationCallback(
        val_samples, cmap="RdYlBu_r", vmin=None, vmax=None, num_samples=4
    )

    # Create progress bar callback
    progress_bar = TQDMProgressBar(refresh_rate=20)

    # Create checkpoint callback
    checkpoint_callback = ModelCheckpoint(
        dirpath=f"{root_dir}checkpoint/",
        filename="unet_smp-{epoch:02d}-{val_mse_loss:.4f}",
        save_top_k=2,
        monitor="val_mse_loss",
    )

    # checkpoint_callback.best_model_path
    logger = TensorBoardLogger(f"{root_dir}logs/tb_logs", name="unet_smp")
    logger.log_hyperparams(hparams)

    # Create trainer
    trainer = L.Trainer(
        max_epochs=hparams["max_epochs"],
        accelerator="gpu",
        callbacks=[progress_bar, visualization_callback, checkpoint_callback],
        logger=logger,
    )

    # # Create tuner
    # tuner = Tuner(trainer)

    # Find optimal learning rate
    # lr_finder = tuner.lr_find(model, datamodule=data_module)
    # new_lr = lr_finder.suggestion()
    # model.learning_rate = new_lr
    # print(f"Suggested Learning Rate: {new_lr}")

    # # Find optimal batch size
    # batch_size_finder = tuner.scale_batch_size(model, datamodule=data_module, mode='power')
    # new_batch_size = data_module.batch_size
    # print(f"Suggested Batch Size: {new_batch_size}")

    # # Update hparams with new values
    # hparams['learning_rate'] = new_lr
    # hparams['batch_size'] = new_batch_size

    # Log updated hyperparameters
    logger.log_hyperparams(hparams)

    # Train the model
    trainer.fit(model, data_module)

    # predictions = trainer.predict(dataloaders=predict_dataloader)
