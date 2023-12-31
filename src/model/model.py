import datetime
import json
import logging
import os.path
from abc import ABC
from time import strftime

import torch
import torch.nn as nn
import numpy as np
from matplotlib import pyplot as plt

from dataloader.custom_dataset import CustomDataset
from dataloader.dataloader import DataLoader
from model.base_model import BaseModel
from model.network import Network


class LSTM(BaseModel, ABC):
    def __init__(self, config):
        super().__init__(config)
        self.model = None
        self.optimizer = None
        self.criterion = nn.CrossEntropyLoss(
            weight=torch.FloatTensor(self.config.train.loss_weight).to(self.device),
            reduction='mean'
        )

        self.train_data, self.val_data = {'files': [], 'labels': []}, {'files': [], 'labels': []}
        self.train_loss, self.val_loss = [], []

        self.save_to = os.path.join(self.config.root_dir, 'saved', strftime('%Y-%m-%d-%H-%M'))
        os.makedirs(self.save_to, exist_ok=True)

        logging.basicConfig(filename=os.path.join(self.save_to, 'train.log'), level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        self.logger.info(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.logger.info('config = \n{}'.format(json.dumps(config, indent=4)))

    def load_data(self) -> None:
        data = DataLoader(self.config).load_data()
        self.train_data, self.val_data = data['train'], data['val']

    def build(self) -> None:
        self.model = Network(self.config)
        self.model.to(self.device)
        self.logger.info(self.model)

    def train(self) -> None:
        optimizer = torch.optim.Adam(params=self.model.parameters(), lr=self.config.train.lr)
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer=optimizer, gamma=self.config.train.gamma)

        train_dataloader = torch.utils.data.DataLoader(
            CustomDataset(config=self.config, data=self.train_data),
            batch_size=self.config.train.train_batch_size,
            shuffle=True,
            num_workers=4,
            pin_memory=True
        )
        val_dataloader = torch.utils.data.DataLoader(
            CustomDataset(config=self.config, data=self.val_data),
            batch_size=self.config.train.val_batch_size,
            shuffle=True,
            num_workers=4,
            pin_memory=True
        )

        train_samples_count = len(self.train_data['files'])
        val_samples_count = len(self.val_data['files'])
        self.logger.info(f'Length of train sequence: {train_samples_count}')
        self.logger.info(f'Length of validate sequence: {val_samples_count}')

        self.logger.info('Training started.')

        highest_acc = 0.
        for epoch in range(self.config.train.epoch):
            train_loss_sum = 0.
            self.model.train()
            for batch_index, batch in enumerate(train_dataloader):
                x, y = batch[0].to(self.device), batch[1].to(self.device)
                optimizer.zero_grad()
                output = self.model(x)
                loss = self.criterion(output, y)
                loss.backward()
                optimizer.step()
                train_loss_sum += loss.item()

                prediction = torch.argmax(output, dim=1)
                acc = (prediction == y).float().mean()

                self.logger.info(
                    'Epoch [{:0>3d}/{:0>3d}] ({:0>3d}/{:0>3d}), Loss : {:.8f}, Accuracy : {:.8f}'.format(
                        epoch + 1, self.config.train.epoch, batch_index + 1, len(train_dataloader), loss.item(), acc
                    )
                )
            self.train_loss.append(train_loss_sum / len(train_dataloader))
            self.logger.info('Current Learning Rate: {:.8f}'.format(scheduler.get_last_lr()[0]))
            scheduler.step()

            # Validation
            val_loss_sum, val_acc_sum, val_acc_mean = 0., 0., 0.
            self.model.eval()
            with torch.no_grad():
                for _, batch in enumerate(val_dataloader):
                    x, y = batch[0].to(self.device), batch[1].to(self.device)
                    output = self.model(x)
                    loss = self.criterion(output, y)
                    val_loss_sum += loss.item()

                    prediction = torch.argmax(output, dim=1)
                    val_acc_sum += (prediction == y).sum().item()

                # Get the mean validation acc of current epoch
                val_acc_mean = val_acc_sum / val_samples_count
                self.logger.info(
                    'Validation, Average Loss : {:.8f}, Accuracy : {:.8f}'.format(
                        val_loss_sum / len(val_dataloader), val_acc_mean
                    )
                )

            self.val_loss.append(val_loss_sum / len(val_dataloader))

            # Log loss
            self.log_loss(epoch)

            # Save the best model
            if highest_acc < val_acc_mean:
                self.logger.info('Highest accuracy was updated from {:.8f} to {:.8f}'.format(
                    highest_acc, val_acc_mean)
                )
                highest_acc = val_acc_mean
                self.save_model(self.model)

        self.logger.info('Training finished.')

    def log_loss(self, epoch: int) -> None:
        loss_path = os.path.join(self.save_to, 'loss.log')
        fig_path = os.path.join(self.save_to, 'loss.png')

        loss = np.column_stack((self.train_loss, self.val_loss))
        np.savetxt(loss_path, loss, fmt='%.8f', delimiter=',')

        plt.gcf().set_size_inches(8, 6)
        plt.xlabel('epoch')
        plt.ylabel('loss')
        plt.plot(np.linspace(0, epoch, epoch + 1).tolist(), self.train_loss, label='train loss')
        plt.plot(np.linspace(0, epoch, epoch + 1).tolist(), self.val_loss, label='val loss')
        plt.legend(['train loss', 'val loss'])
        plt.savefig(fig_path, bbox_inches='tight', dpi=600)
        plt.clf()

    def save_model(self, model: torch.nn.Module) -> None:
        model_path = os.path.join(self.save_to, 'best_model.pth')
        jit_model_path = os.path.join(self.save_to, 'best_model.pt')

        # Save the whole model for continuous training
        torch.save(model, model_path)

        # Save script model for torchserve inference
        script_model = Network(self.config)
        script_model.load_state_dict(model.state_dict())
        for parameter in script_model.parameters():
            parameter.requires_grad = False
        script_model.eval()
        traced_model = torch.jit.trace(script_model,
                                       torch.rand(1, 1, self.config.model.input_size))
        traced_model.save(jit_model_path)
        self.logger.info(f'Model saved to {model_path}')
