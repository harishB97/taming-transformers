#based on https://github.com/CompVis/taming-transformers

from scripts.import_utils import instantiate_from_config
import torch
import torch.nn.functional as F
import pytorch_lightning as pl

from scripts.modules.util import SOSProvider

import scripts.constants as CONSTANTS

from torchmetrics import F1Score

def disabled_train(self, mode=True):
    """Overwrite model.train with this function to make sure train/eval mode
    does not change anymore."""
    return self


class Net2NetTransformer(pl.LightningModule):
    def __init__(self,
                 transformer_config,
                 first_stage_config,
                 cond_stage_config,
                 permuter_config=None,
                 ckpt_path=None,
                 ignore_keys=[],
                 first_stage_key="image",
                 cond_stage_key="depth",
                 downsample_cond_size=-1,
                 pkeep=1.0,
                 sos_token=0,
                 unconditional=False,
                 ):
        super().__init__()
        self.be_unconditional = unconditional
        self.sos_token = sos_token
        self.first_stage_key = first_stage_key
        self.cond_stage_key = cond_stage_key
        self.init_first_stage_from_ckpt(first_stage_config)
        self.init_cond_stage_from_ckpt(cond_stage_config)
        if permuter_config is None:
            permuter_config = {"target": "scripts.modules.transformer.permuter.Identity"}
        self.permuter = instantiate_from_config(config=permuter_config)
        self.transformer = instantiate_from_config(config=transformer_config)

        if ckpt_path is not None:
            self.init_from_ckpt(ckpt_path, ignore_keys=ignore_keys)
        self.downsample_cond_size = downsample_cond_size
        self.pkeep = pkeep

    def init_from_ckpt(self, path, ignore_keys=list()):
        sd = torch.load(path, map_location="cpu")["state_dict"]
        for k in sd.keys():
            for ik in ignore_keys:
                if k.startswith(ik):
                    self.print("Deleting key {} from state_dict.".format(k))
                    del sd[k]
        self.load_state_dict(sd, strict=False)
        print(f"Restored from {path}")

    def init_first_stage_from_ckpt(self, config):
        model = instantiate_from_config(config)
        model = model.eval()
        model.train = disabled_train
        self.first_stage_model = model

    def init_cond_stage_from_ckpt(self, config):
        if config == "__is_first_stage__":
            print("Using first stage also as cond stage.")
            self.cond_stage_model = self.first_stage_model
        elif config == "__is_unconditional__" or self.be_unconditional:
            print(f"Using no cond stage. Assuming the training is intended to be unconditional. "
                  f"Prepending {self.sos_token} as a sos token.")
            self.be_unconditional = True
            self.cond_stage_key = self.first_stage_key
            self.cond_stage_model = SOSProvider(self.sos_token)
        else:
            model = instantiate_from_config(config)
            model = model.eval()
            model.train = disabled_train
            self.cond_stage_model = model

    def forward(self, x, c):
        _, z_indices = self.encode_to_z(x)
        _, c_indices = self.encode_to_c(c)
        
        if isinstance(self, PhyloNN_transformer):
            if not self.be_unconditional:
                num_phylo_features = self.first_stage_model.phylo_disentangler.n_phylolevels * self.first_stage_model.phylo_disentangler.codebooks_per_phylolevel
                z_indices_phylo = z_indices[:, :num_phylo_features]
                z_indices_phylo_sub = self.first_stage_model.phylo_disentangler.embedding_converter.get_level(z_indices_phylo, self.cond_stage_model.level)
                z_indices = z_indices_phylo_sub

        if self.training and self.pkeep < 1.0:
            mask = torch.bernoulli(self.pkeep*torch.ones(z_indices.shape,
                                                         device=z_indices.device))
            mask = mask.round().to(dtype=torch.int64)
            r_indices = torch.randint_like(z_indices, self.transformer.config.vocab_size)
            a_indices = mask*z_indices+(1-mask)*r_indices
        else:
            a_indices = z_indices

        cz_indices = torch.cat((c_indices, a_indices), dim=1)

        # target includes all sequence elements (no need to handle first one
        # differently because we are conditioning)
        target = z_indices
        # make the prediction
        logits, _ = self.transformer(cz_indices[:, :-1])
        # cut off conditioning outputs - output i corresponds to p(z_i | z_{<i}, c)
        logits = logits[:, c_indices.shape[1]-1:]

        return logits, target

    def top_k_logits(self, logits, k):
        v, ix = torch.topk(logits, k)
        out = logits.clone()
        out[out < v[..., [-1]]] = -float('Inf')
        return out

    #NOTE: When sampling we need to make sure that passed x follows the embedding convension. Same goes for the output
    @torch.no_grad()
    def sample(self, x, c, steps, temperature=1.0, sample=False, top_k=None,
               callback=lambda k: None):
        x = torch.cat((c,x),dim=1)
        block_size = self.transformer.get_block_size()
        # assert not self.transformer.training
        if self.pkeep <= 0.0:
            # one pass suffices since input is pure noise anyway
            assert len(x.shape)==2
            noise = c.clone()[:,x.shape[1]-c.shape[1]:-1]
            x = torch.cat((x,noise),dim=1)
            logits, _ = self.transformer(x)
            # take all logits for now and scale by temp
            logits = logits / temperature
            # optionally crop probabilities to only the top k options
            if top_k is not None:
                logits = self.top_k_logits(logits, top_k)
            # apply softmax to convert to probabilities
            probs = F.softmax(logits, dim=-1)
            # sample from the distribution or take the most likely
            if sample:
                shape = probs.shape
                probs = probs.reshape(shape[0]*shape[1],shape[2])
                ix = torch.multinomial(probs, num_samples=1)
                probs = probs.reshape(shape[0],shape[1],shape[2])
                ix = ix.reshape(shape[0],shape[1])
            else:
                _, ix = torch.topk(probs, k=1, dim=-1)
            # cut off conditioning
            x = ix[:, c.shape[1]-1:]
        else:
            for k in range(steps):
                callback(k)
                assert x.size(1) <= block_size, "x.size(1) {0}, block_size {1}".format(x.size(1), block_size) # make sure model can see conditioning
                x_cond = x if x.size(1) <= block_size else x[:, -block_size:]  # crop context if needed
                logits, _ = self.transformer(x_cond)
                # pluck the logits at the final step and scale by temperature
                logits = logits[:, -1, :] / temperature
                # optionally crop probabilities to only the top k options
                if top_k is not None:
                    logits = self.top_k_logits(logits, top_k)
                # apply softmax to convert to probabilities
                probs = F.softmax(logits, dim=-1)
                # sample from the distribution or take the most likely
                if sample:
                    ix = torch.multinomial(probs, num_samples=1)
                else:
                    _, ix = torch.topk(probs, k=1, dim=-1)
                # append to the sequence and continue
                x = torch.cat((x, ix), dim=1)
            # cut off conditioning
            x = x[:, c.shape[1]:]
        return x

    @torch.no_grad()
    def encode_to_z(self, x):
        quant_z, _, info = self.first_stage_model.encode(x)
        info = info[2]
        indices = info.view(quant_z.shape[0], -1)
        indices = self.permuter(indices)
        return quant_z, indices

    @torch.no_grad()
    def encode_to_c(self, c):
        if self.downsample_cond_size > -1:
            c = F.interpolate(c, size=(self.downsample_cond_size, self.downsample_cond_size))
        quant_c, _, [_,_,indices] = self.cond_stage_model.encode(c)
        if len(indices.shape) > 2:
            indices = indices.view(c.shape[0], -1)
        return quant_c, indices

    @torch.no_grad()
    def decode_to_img(self, index, zshape):
        index = self.permuter(index, reverse=True)
        bhwc = (zshape[0],zshape[2],zshape[3],zshape[1])
        quant_z = self.first_stage_model.quantize.get_codebook_entry(
            index.reshape(-1), shape=bhwc)
        x = self.first_stage_model.decode(quant_z)
        return x

    @torch.no_grad()
    def log_images(self, batch, temperature=None, top_k=None, callback=None, lr_interface=False, split="train", **kwargs):
        log = dict()
        
        N = 4
        if lr_interface:
            x, c = self.get_xc(batch, N, diffuse=False, upsample_factor=8)
        else:
            x, c = self.get_xc(batch, N)
        x = x.to(device=self.device)
        c = c.to(device=self.device)

        quant_z, z_indices = self.encode_to_z(x)
        quant_c, c_indices = self.encode_to_c(c)

        # sample
        z_start_indices = z_indices[:, :0]
        if isinstance(self, PhyloNN_transformer) and self.PhyloNN_transformer:
            codebooks_per_phylolevel = self.first_stage_model.phylo_disentangler.codebooks_per_phylolevel
            n_phylolevels = self.first_stage_model.phylo_disentangler.n_phylolevels
            attr_codes_range = codebooks_per_phylolevel*n_phylolevels
            z_indices_samples = z_indices[:, attr_codes_range:]
        else:
            z_indices_samples = z_indices.clone()
        index_sample = self.sample(z_start_indices, c_indices,
                                steps=z_indices_samples.shape[1],
                                temperature=temperature if temperature is not None else 1.0,
                                sample=True,
                                top_k=top_k if top_k is not None else 100,
                                callback=callback if callback is not None else lambda k: None)
        if isinstance(self, PhyloNN_transformer) and self.PhyloNN_transformer:
            codebooks_per_phylolevel = self.first_stage_model.phylo_disentangler.codebooks_per_phylolevel
            n_levels_non_attribute = self.first_stage_model.phylo_disentangler.n_levels_non_attribute
            index_sample = torch.cat([c_indices[:, -n_levels_non_attribute*codebooks_per_phylolevel:], index_sample], dim=-1)
        x_sample_nopix = self.decode_to_img(index_sample, quant_z.shape)

        # det sample
        z_start_indices = z_indices[:, :0]
        index_sample = self.sample(z_start_indices, c_indices,
                                steps=z_indices_samples.shape[1],
                                sample=False,
                                callback=callback if callback is not None else lambda k: None)
        if isinstance(self, PhyloNN_transformer) and self.PhyloNN_transformer:
            codebooks_per_phylolevel = self.first_stage_model.phylo_disentangler.codebooks_per_phylolevel
            n_levels_non_attribute = self.first_stage_model.phylo_disentangler.n_levels_non_attribute
            index_sample = torch.cat([c_indices[:, -n_levels_non_attribute*codebooks_per_phylolevel:], index_sample], dim=-1)
        x_sample_det = self.decode_to_img(index_sample, quant_z.shape)

        # reconstruction
        x_rec = self.decode_to_img(z_indices, quant_z.shape)

        log["inputs"] = x
        log["reconstructions"] = x_rec

        if self.cond_stage_key in ["objects_bbox", "objects_center_points"]:
            figure_size = (x_rec.shape[2], x_rec.shape[3])
            dataset = kwargs["pl_module"].trainer.datamodule.datasets["validation"]
            label_for_category_no = dataset.get_textual_label_for_category_no
            plotter = dataset.conditional_builders[self.cond_stage_key].plot
            log["conditioning"] = torch.zeros_like(log["reconstructions"])
            for i in range(quant_c.shape[0]):
                log["conditioning"][i] = plotter(quant_c[i], label_for_category_no, figure_size)
            log["conditioning_rec"] = log["conditioning"]
        elif self.cond_stage_key != "image":
            cond_rec = self.cond_stage_model.decode(quant_c)
            if self.cond_stage_key == "segmentation":
                # get image from segmentation mask
                num_classes = cond_rec.shape[1]

                c = torch.argmax(c, dim=1, keepdim=True)
                c = F.one_hot(c, num_classes=num_classes)
                c = c.squeeze(1).permute(0, 3, 1, 2).float()
                c = self.cond_stage_model.to_rgb(c)

                cond_rec = torch.argmax(cond_rec, dim=1, keepdim=True)
                cond_rec = F.one_hot(cond_rec, num_classes=num_classes)
                cond_rec = cond_rec.squeeze(1).permute(0, 3, 1, 2).float()
                cond_rec = self.cond_stage_model.to_rgb(cond_rec)
                
        log["samples_nopix"] = x_sample_nopix
        log["samples_det"] = x_sample_det
        return log

    def get_input(self, key, batch):
        x = batch[key]
        if len(x.shape) == 3:
            x = x[..., None]
        if len(x.shape) == 4:
            x = x.permute(0, 3, 1, 2).to(memory_format=torch.contiguous_format)
        if x.dtype == torch.double:
            x = x.float()
        return x

    def get_xc(self, batch, N=None):
        x = self.get_input(self.first_stage_key, batch)
        c = self.get_input(self.cond_stage_key, batch)
        
        if isinstance(self, PhyloNN_transformer) and self.PhyloNN_transformer:
            zq_phylo, _, _, _, _, _, _, _ = self.first_stage_model.encode(x.to(device=self.device))
            zq_phylo = self.first_stage_model.phylo_disentangler.embedding_converter.get_phylo_codes(zq_phylo, verify=False)
            c =torch.cat((c.view(c.shape[0], 1).to(device=self.device), zq_phylo), 1)
            
            
        if N is not None:
            x = x[:N]
            c = c[:N]
        return x, c

    def shared_step(self, batch, batch_idx):
        x, c = self.get_xc(batch)
        logits, target = self(x, c)
        loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)), target.reshape(-1))
        return loss

    def training_step(self, batch, batch_idx):
        loss = self.shared_step(batch, batch_idx)
        self.log("train/loss", loss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return loss

    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        loss = self.shared_step(batch, batch_idx)
        self.log("val/loss", loss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return loss

    def configure_optimizers(self):
        """
        Following minGPT:
        This long function is unfortunately doing something very simple and is being very defensive:
        We are separating out all parameters of the model into two buckets: those that will experience
        weight decay for regularization and those that won't (biases, and layernorm/embedding weights).
        We are then returning the PyTorch optimizer object.
        """

        # separate out all parameters to those that will and won't experience regularizing weight decay
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear, )
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.Embedding)
        for mn, m in self.transformer.named_modules():
            for pn, p in m.named_parameters():
                fpn = '%s.%s' % (mn, pn) if mn else pn # full param name

                if pn.endswith('bias'):
                    # all biases will not be decayed
                    no_decay.add(fpn)
                elif pn.endswith('weight') and isinstance(m, whitelist_weight_modules):
                    # weights of whitelist modules will be weight decayed
                    decay.add(fpn)
                elif pn.endswith('weight') and isinstance(m, blacklist_weight_modules):
                    # weights of blacklist modules will NOT be weight decayed
                    no_decay.add(fpn)

        # special case the position embedding parameter in the root GPT module as not decayed
        no_decay.add('pos_emb')

        # validate that we considered every parameter
        param_dict = {pn: p for pn, p in self.transformer.named_parameters()}
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert len(inter_params) == 0, "parameters %s made it into both decay/no_decay sets!" % (str(inter_params), )
        assert len(param_dict.keys() - union_params) == 0, "parameters %s were not separated into either decay/no_decay set!" \
                                                    % (str(param_dict.keys() - union_params), )

        # create the pytorch optimizer object
        optim_groups = [
            {"params": [param_dict[pn] for pn in sorted(list(decay))], "weight_decay": 0.01},
            {"params": [param_dict[pn] for pn in sorted(list(no_decay))], "weight_decay": 0.0}
        ]
        optimizer = torch.optim.AdamW(optim_groups, lr=self.learning_rate, betas=(0.9, 0.95))

        
        lr_scheduler = {
            "scheduler": torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=50, mode='min', factor=0.33, min_lr=self.learning_rate*0.1, verbose=True), 
            "monitor": "val"+CONSTANTS.TRANSFORMER_LOSS
            }
            
        return [optimizer], [lr_scheduler]
    
    
    
#***************
class PhyloNN_transformer(Net2NetTransformer):
    def __init__(self, **args):
        print(args)
        
        # For wandb
        self.save_hyperparameters()
        
        self.top_k = 100
        if "top_k" in args.keys():
            self.top_k = args["top_k"]
            del args["top_k"]
        
        super().__init__(**args)

        self.first_stage_model.freeze()
        
        self.outputname = CONSTANTS.DISENTANGLER_CLASS_OUTPUT
        self.F1 =self.first_stage_model.phylo_disentangler.loss_phylo.F1
        if not self.be_unconditional and self.cond_stage_model.phylo_mapper is not None:
            self.outputname = self.cond_stage_model.phylo_mapper.outputname
            self.F1 = F1Score(num_classes=self.first_stage_model.phylo_disentangler.loss_phylo.classifier_output_sizes[self.cond_stage_model.phylo_mapper.level], multiclass=True) 
                
    @torch.no_grad()
    def encode_to_z(self, x):
        zq_phylo, zq_nonphylo, _, _, _, _, info_attr, info_nonattr = self.first_stage_model.encode(x)
        info_attr=info_attr[2]
        info_nonattr=info_nonattr[2]
        quant_z = torch.cat((zq_phylo,zq_nonphylo), dim=3)
        indices_attr = info_attr.view(quant_z.shape[0], -1)
        indices_nonattr = info_nonattr.view(quant_z.shape[0], -1)
        indices = torch.cat((indices_attr,indices_nonattr), dim=1)
        indices = self.permuter(indices)
        return quant_z, indices
    
    def assert_can_decode_into_image(self, index, zshape, assert_=False):
        codebooks_per_phylolevel = self.first_stage_model.phylo_disentangler.codebooks_per_phylolevel
        n_phylolevels = self.first_stage_model.phylo_disentangler.n_phylolevels
        n_levels_non_attribute = self.first_stage_model.phylo_disentangler.n_levels_non_attribute
        attr_codes_range = codebooks_per_phylolevel*n_phylolevels
        nonattr_codes_range = codebooks_per_phylolevel*n_levels_non_attribute
        
        if assert_:
            assert index.shape[1] == attr_codes_range + nonattr_codes_range
            assert zshape[3] == n_phylolevels + n_levels_non_attribute
        return (index.shape[1] == attr_codes_range + nonattr_codes_range) and (zshape[3] == n_phylolevels + n_levels_non_attribute)
    
    def decode_to_img(self, index, zshape):
        self.assert_can_decode_into_image(index, zshape, assert_=True)

        index = self.permuter(index, reverse=True)
        
        codebooks_per_phylolevel = self.first_stage_model.phylo_disentangler.codebooks_per_phylolevel
        n_phylolevels = self.first_stage_model.phylo_disentangler.n_phylolevels
        n_levels_non_attribute = self.first_stage_model.phylo_disentangler.n_levels_non_attribute
        attr_codes_range = codebooks_per_phylolevel*n_phylolevels

        index_attr = index[:, :attr_codes_range]
        index_nonattr = index[:, attr_codes_range:]
        bhwc = (zshape[0],zshape[2],n_phylolevels,zshape[1])
        bhwc_nonattr = (zshape[0],zshape[2],n_levels_non_attribute,zshape[1])
        
        zq_phylo = self.first_stage_model.phylo_disentangler.quantize.get_codebook_entry(
            index_attr.reshape(-1), shape=bhwc)
        zq_nonphylo = self.first_stage_model.phylo_disentangler.quantize.get_codebook_entry(
            index_nonattr.reshape(-1), shape=bhwc_nonattr)
        x, _, _, _ = self.first_stage_model.decode(zq_phylo, zq_nonphylo)
        
        return x

    def log_images(self, batch, temperature=None, top_k=None, callback=None, lr_interface=False, **kwargs):
        if top_k is None:
            top_k = self.top_k
        return super().log_images(batch, temperature, top_k, callback, lr_interface, **kwargs)
    
    def shared_step(self, batch, batch_idx, split='train'):
        loss = super().shared_step(batch, batch_idx)
        
        if batch_idx%100==0:
            with torch.no_grad():
                x, c = self.get_xc(batch)
                x = x.to(device=self.device)
                c = c.to(device=self.device)
                quant_z, z_indices = self.encode_to_z(x)
                quant_c, c_indices = self.encode_to_c(c)
                
                # sample
                z_start_indices = z_indices[:, :0]
                index_sample = self.sample(z_start_indices, c_indices,
                                        steps=z_indices.shape[1],
                                        temperature=1.0,
                                        sample=True,
                                        top_k=self.top_k)
                x_sample_nopix = self.decode_to_img(index_sample, quant_z.shape)

                # det sample
                z_start_indices = z_indices[:, :0]
                index_sample = self.sample(z_start_indices, c_indices,
                                        steps=z_indices.shape[1],
                                        sample=False)
                if self.PhyloNN_transformer:
                    codebooks_per_phylolevel = self.first_stage_model.phylo_disentangler.codebooks_per_phylolevel
                    n_levels_non_attribute = self.first_stage_model.phylo_disentangler.n_levels_non_attribute
                    index_sample = torch.cat([c_indices[:, -n_levels_non_attribute*codebooks_per_phylolevel:], index_sample], dim=-1)
                x_sample_det = self.decode_to_img(index_sample, quant_z.shape)
                    
                truth = quant_c[:, 0]
                if not self.be_unconditional and self.cond_stage_model.phylo_mapper is not None:
                    truth = self.cond_stage_model.phylo_mapper.get_mapped_truth(truth)
                    
                f1_samples_nopix = self.F1(self.first_stage_model(x_sample_nopix)[3][self.outputname], truth)
                f1_x_sample_det = self.F1(self.first_stage_model(x_sample_det)[3][self.outputname], truth)
                self.log(split+"/f1_samples_nopix", f1_samples_nopix, prog_bar=False, logger=True, on_step=False, on_epoch=True)
                self.log(split+"/f1_x_sample_det", f1_x_sample_det, prog_bar=False, logger=True, on_step=False, on_epoch=True)

        return loss

    def training_step(self, batch, batch_idx):
        loss = self.shared_step(batch, batch_idx, split='train')
        self.log("train/loss", loss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return loss

    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        loss = self.shared_step(batch, batch_idx, split='val')
        self.log("val/loss", loss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
        return loss