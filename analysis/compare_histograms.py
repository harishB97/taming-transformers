from scripts.loading_utils import load_config, load_model
from scripts.data.custom import CustomTest as CustomDataset
from scripts.analysis_utils import Embedding_Code_converter, HistogramParser
from scripts.plotting_utils import dump_to_json, get_fig_pth
import scripts.constants as CONSTANTS

import torch
import os
from omegaconf import OmegaConf
import argparse
import pickle

##########

def generate_report(species_names, ckpt_path, attr_info, nonattr_info, converter, hist_parser):
    attr_distances, attr_most_common1, attr_most_common2 = attr_info  
    nonattr_distances, non_attr_most_common1, non_attr_most_common2 = nonattr_info 
    
    all_distances = torch.cat((attr_distances, nonattr_distances), dim=0)
    
    average_distance = torch.mean(all_distances)
    
    average_distance_attr = torch.mean(attr_distances)
    average_distance_levels = []
    sub_common_levels = []
    for i in range(hist_parser.n_phylolevels-1):
        sub_distances = converter.get_sub_level(attr_distances.unsqueeze(0), i) 
        sub_common1 = converter.get_sub_level(attr_most_common1.unsqueeze(0), i) 
        sub_common2 = converter.get_sub_level(attr_most_common2.unsqueeze(0), i) 
        sub_common_levels.append([sub_common1, sub_common2])
        average_distance_levels.append(torch.mean(sub_distances))
        
    average_distance_nonattr = torch.mean(nonattr_distances)
    
    json = {
        "average distance of all codes": average_distance.item(),
        "average distance of phylo codes": average_distance_attr.item(),
        "average distance of non-phylo codes": average_distance_nonattr.item(),
        "average distance of different levels": {},
        "phylo codes": {
            species_names[0]:  attr_most_common1.tolist(),
            species_names[1]:  attr_most_common2.tolist()
        },
        "non-phylo codes": {
            species_names[0]:  non_attr_most_common1.tolist(),
            species_names[1]:  non_attr_most_common2.tolist()
        },
        "phylo codes levels": {}
    }
    for i in range(hist_parser.n_phylolevels-1):
        json["average distance of different levels"][i] = average_distance_levels[i].item()
    for i in range(hist_parser.n_phylolevels-1):
        json["phylo codes levels"][i] = {
            species_names[0]: sub_common_levels[i][0].tolist(),
            species_names[1]: sub_common_levels[i][1].tolist()
        } 
    
    name = "distances between "+species_names[0]+" and "+ species_names[1]
    dump_to_json(json, ckpt_path, name=name)
    print(name, json)

#*****************

@torch.no_grad()
def main(configs_yaml):
    yaml_path = configs_yaml.yaml_path
    ckpt_path = configs_yaml.ckpt_path
    DEVICE = configs_yaml.DEVICE
    species1_name = configs_yaml.species1
    species2_name = configs_yaml.species2
    size = configs_yaml.size
    file_list_path = configs_yaml.file_list_path

    dataset = CustomDataset(size, file_list_path, add_labels=True)
    
    # Load model
    config = load_config(yaml_path, display=False)
    model = load_model(config, ckpt_path=ckpt_path, cuda=(DEVICE is not None))
    
    # load histograms
    histograms_file = os.path.join(get_fig_pth(ckpt_path, postfix=CONSTANTS.HISTOGRAMS_FOLDER), CONSTANTS.HISTOGRAMS_FILE)
    histogram_file_exists = os.path.exists(histograms_file)
    if not histogram_file_exists:
        raise "histograms have not been generated. Run code_histogram.py first! Defaulting to index ordering"
    hist_arr, hist_arr_nonattr = pickle.load(open(histograms_file, "rb"))
    
    # parse histograms
    hist_parser = HistogramParser(model)
    converter = Embedding_Code_converter(model.phylo_disentangler.quantize.get_codebook_entry_index, model.phylo_disentangler.quantize.embedding, (1, model.phylo_disentangler.embed_dim, hist_parser.codes_per_phylolevel, hist_parser.n_phylolevels))
    
    species1_indx = dataset.labels_to_idx[species1_name]
    species2_indx = dataset.labels_to_idx[species2_name]
    
    attr_info = hist_parser.get_distances(hist_arr, species1_indx, species2_indx)
    nonattr_info = hist_parser.get_distances(hist_arr_nonattr, species1_indx, species2_indx)
    
    generate_report([species1_name, species2_name], ckpt_path, attr_info, nonattr_info, converter, hist_parser)
    
    
    
  

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-n",
        "--config",
        type=str,
        nargs="?",
        const=True,
        default="analysis/configs/compare_histograms.yaml",
    )
    
    cfg, _ = parser.parse_known_args()
    configs = OmegaConf.load(cfg.config)
    cli = OmegaConf.from_cli()
    config = OmegaConf.merge(configs, cli)
    print(config)
    
    main(config)