# -*- Python -*-

# A simple example of using WebDataset for ImageNet training.
# This uses the PyTorch Lightning framework.

# Loosly based on 
# https://github.com/PyTorchLightning/pytorch-lightning/blob/master/pl_examples/domain_templates/imagenet.py

import os.path
import torch
import torchvision
from torchvision import transforms
from torch.nn import functional as F
from torch.optim import lr_scheduler
from argparse import ArgumentParser

import pytorch_lightning as pl
from pytorch_lightning import plugins
import webdataset as wds


torchvision


def identity(x):
    return x


class ImagenetData(pl.LightningDataModule):
    def __init__(self, shards=None, valshards=None, batch_size=64, workers=4, bucket=None, **kw):
        super().__init__(self)
        self.normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        self.training_urls = os.path.join(bucket, shards)
        print("training_urls = ", self.training_urls)
        self.val_urls = os.path.join(bucket, valshards)
        print("val_urls = ", self.val_urls)
        self.batch_size = batch_size
        self.num_workers = workers
        self.world_size = 0
        if torch.distributed.is_initialized():
            self.world_size = torch.distributed.get_world_size()
        print("batch_size", self.batch_size, "num_workers", self.num_workers, "world_size", self.world_size)

    def make_transform(self, mode="train"):
        if mode == "train":
            return transforms.Compose(
                [
                    transforms.RandomResizedCrop(224),
                    transforms.RandomHorizontalFlip(),
                    transforms.ToTensor(),
                    self.normalize,
                ]
            )
        elif mode == "val":
            return transforms.Compose(
                [
                    transforms.Resize(256),
                    transforms.CenterCrop(224),
                    transforms.ToTensor(),
                    self.normalize,
                ]
            )

    def make_loader(self, urls, mode="train"):
        if mode == "train":
            dataset_size = 1281167
            shuffle = 5000
        elif mode == "val":
            dataset_size = 5000
            shuffle = 0

        transform = self.make_transform(mode=mode)

        dataset = (
            wds.WebDataset(urls)
            .shuffle(shuffle)
            .decode("pil")
            .to_tuple("jpg;png;jpeg cls")
            .map_tuple(transform, identity)
            .batched(self.batch_size, partial=False)
        )

        loader = wds.WebLoader(
            dataset,
            batch_size=None,
            shuffle=False,
            num_workers=self.num_workers,
        )
        loader.length = dataset_size // self.batch_size

        if self.world_size > 0:
            number_of_batches = dataset_size // (self.batch_size * self.world_size)
            print("# batches per node = ", number_of_batches)
            loader = loader.repeat(2).slice(number_of_batches)
            loader.length = number_of_batches

        return loader

    def train_dataloader(self):
        return self.make_loader(self.training_urls, mode="train")

    def val_dataloader(self):
        return self.make_loader(self.val_urls, mode="val")

    @staticmethod
    def add_loader_specific_args(parser):
        parser.add_argument("-b", "--batch-size", type=int, default=128)
        parser.add_argument("--workers", type=int, default=6)
        parser.add_argument("--bucket", default="./shards")
        parser.add_argument("--shards", default="imagenet-train-{000000..000146}.tar")
        parser.add_argument("--valshards", default="imagenet-val-{000000..000006}.tar")
        return parser


class ImageClassifier(pl.LightningModule):
    def __init__(self, learning_rate=0.1, momentum=0.9, weight_decay=1e-4, model="resnet18", **kw):
        super().__init__()
        self.save_hyperparameters()
        self.model = eval(f"torchvision.models.{model}")()

    def forward(self, inputs):
        return self.model(inputs)

    def training_step(self, batch, batch_idx):
        images, target = batch
        output = self(images)
        loss_train = F.cross_entropy(output, target)
        acc1, acc5 = self.__accuracy(output, target, topk=(1, 5))
        self.log("train_loss", loss_train, on_step=True, on_epoch=True, logger=True)
        self.log("train_acc1", acc1, on_step=True, prog_bar=True, on_epoch=True, logger=True)
        self.log("train_acc5", acc5, on_step=True, on_epoch=True, logger=True)
        return loss_train

    def validation_step(self, batch, batch_idx):
        images, target = batch
        output = self(images)
        loss_val = F.cross_entropy(output, target)
        acc1, acc5 = self.__accuracy(output, target, topk=(1, 5))
        self.log("val_loss", loss_val, on_step=True, on_epoch=True)
        self.log("val_acc1", acc1, on_step=True, prog_bar=True, on_epoch=True)
        self.log("val_acc5", acc5, on_step=True, on_epoch=True)

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(
            self.parameters(),
            lr=self.hparams.learning_rate,
            momentum=self.hparams.momentum,
            weight_decay=self.hparams.weight_decay,
        )
        scheduler = lr_scheduler.LambdaLR(optimizer, lambda epoch: 0.1 ** (epoch // 30))
        return [optimizer], [scheduler]

    def test_dataloader(self):
        return self.val_dataloader()

    def test_step(self, *args, **kwargs):
        return self.validation_step(*args, **kwargs)

    def test_epoch_end(self, *args, **kwargs):
        outputs = self.validation_epoch_end(*args, **kwargs)

        def substitute_val_keys(out):
            return {k.replace("val", "test"): v for k, v in out.items()}

        outputs = {
            "test_loss": outputs["val_loss"],
            "progress_bar": substitute_val_keys(outputs["progress_bar"]),
            "log": substitute_val_keys(outputs["log"]),
        }
        return outputs

    @staticmethod
    def __accuracy(output, target, topk=(1,)):
        """Computes the accuracy over the k top predictions for the specified values of k"""
        with torch.no_grad():
            maxk = max(topk)
            batch_size = target.size(0)

            _, pred = output.topk(maxk, 1, True, True)
            pred = pred.t()
            correct = pred.eq(target.view(1, -1).expand_as(pred))

            res = []
            for k in topk:
                correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
                res.append(correct_k.mul_(100.0 / batch_size))
            return res

    @staticmethod
    def add_model_specific_args(parser):
        parser.add_argument("--learning_rate", type=float, default=0.1)
        parser.add_argument("--momentum", type=float, default=0.9)
        parser.add_argument("--weight_decay", type=float, default=1e-4)
        return parser


def main(args):
    if args.accelerator in ["ddp"]:
        args.batch_size = int(args.batch_size / max(1, args.gpus))
        args.workers = int(args.workers / max(1, args.gpus))
    data = ImagenetData(**vars(args))
    model = ImageClassifier(**vars(args))
    plugin = plugins.DDPPlugin(find_unused_parameters=False)
    trainer = pl.Trainer.from_argparse_args(args, plugins=plugin)
    if args.evaluate:
        trainer.test(model, data)
    else:
        trainer.fit(model, data)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--evaluate", action="store_true")
    parser = pl.Trainer.add_argparse_args(parser)
    parser = ImagenetData.add_loader_specific_args(parser)
    parser = ImageClassifier.add_model_specific_args(parser)
    args = parser.parse_args()
    main(args)
