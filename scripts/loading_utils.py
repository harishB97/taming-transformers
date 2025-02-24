#based on https://github.com/CompVis/taming-transformers

import yaml
from scripts.models.phyloautoencoder import PhyloVQVAE
from omegaconf import OmegaConf
import torch

######### loaders

def load_config(config_path, display=False):
    config = OmegaConf.load(config_path)
    if display:
        print(yaml.dump(OmegaConf.to_container(config)))
    return config


def load_model(config, ckpt_path=None, cuda=False, model_type=PhyloVQVAE):
    model = model_type(**config.model.params)
    if ckpt_path is not None:
        sd = torch.load(ckpt_path, map_location="cpu")["state_dict"]
        missing, unexpected = model.load_state_dict(sd, strict=True)
    if cuda:
        model = model.cuda()
    return model.eval()


