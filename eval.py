# Copyright (c) 2019 NVIDIA Corporation
import argparse
import copy
import os
import pickle

from ruamel.yaml import YAML
import nemo
# import nemo_asr
import nemo.collections.asr as nemo_asr
from nemo.collections.asr.helpers import post_process_predictions, post_process_transcripts
from nemo.collections.asr.metrics import word_error_rate

def main():
    parser = argparse.ArgumentParser(description='Jasper')
    parser.add_argument("--local_rank", default=None, type=int)
    parser.add_argument("--batch_size", default=16, type=int)
    parser.add_argument("--model_config", default="config/quartznet12x1_abcfjwz.yaml", type=str, required=False)
    # parser.add_argument("--model_config", default="config/quartznet12x1.yaml", type=str, required=False)
    parser.add_argument("--load_dir", default='quartznet12x1_abcfjz_them100h/checkpoints', type=str, required=False)
    parser.add_argument("--eval_datasets", default='data/grapheme/test-vlsp2019.json', type=str, required=False)
    # parser.add_argument("--load_dir", default='quartznet12x1_abcfjwz_finetune/checkpoints', type=str, required=False)
    parser.add_argument("--save_logprob", default=False, type=str)
    parser.add_argument("--lm_path", default="NeMo/scripts/language_model2/5-gram-lm.binary", type=str)
    parser.add_argument("--beam_width", default=100, type=int)
    parser.add_argument("--alpha", default=2.0, type=float)
    parser.add_argument("--beta", default=2.5, type=float)
    parser.add_argument("--cutoff_prob", default=0.99, type=float)
    parser.add_argument("--cutoff_top_n", default=40, type=int)

    args = parser.parse_args()
    batch_size = args.batch_size
    load_dir = args.load_dir

    if args.local_rank is not None:
        if args.lm_path:
            raise NotImplementedError(
                "Beam search decoder with LM does not currently support "
                "evaluation on multi-gpu.")
        device = nemo.core.DeviceType.AllGpu
    else:
        device = nemo.core.DeviceType.GPU

    # Instantiate Neural Factory with supported backend
    neural_factory = nemo.core.NeuralModuleFactory(
        backend=nemo.core.Backend.PyTorch,
        local_rank=args.local_rank,
        # optimization_level=nemo.core.Optimization.mxprO1,
        placement=device)
    logger = neural_factory.logger

    if args.local_rank is not None:
        logger.info('Doing ALL GPU')

    yaml = YAML(typ="safe")
    with open(args.model_config) as f:
        jasper_params = yaml.load(f)

    vocab = jasper_params['labels']
    # print(jasper_params)
    sample_rate = jasper_params["AudioToMelSpectrogramPreprocessor"]["sample_rate"]
    eval_datasets = args.eval_datasets

    eval_dl_params = copy.deepcopy(jasper_params["AudioToTextDataLayer"])
    # eval_dl_params.update(jasper_params["AudioToTextDataLayer"]["eval"])
    # eval_dl_params["normalize_transcripts"] = False
    # del eval_dl_params["train"]
    del jasper_params["AudioToMelSpectrogramPreprocessor"]["sample_rate"]
    data_layer = nemo_asr.AudioToTextDataLayer(manifest_filepath=eval_datasets, sample_rate=sample_rate, labels=vocab, batch_size=batch_size, **eval_dl_params)

    n = len(data_layer)
    logger.info('Evaluating {0} examples'.format(n))

    data_preprocessor = nemo_asr.AudioToMelSpectrogramPreprocessor(sample_rate=sample_rate,**jasper_params["AudioToMelSpectrogramPreprocessor"])
    jasper_encoder = nemo_asr.JasperEncoder(feat_in=jasper_params["AudioToMelSpectrogramPreprocessor"]["features"],**jasper_params["JasperEncoder"])
    jasper_decoder = nemo_asr.JasperDecoderForCTC(feat_in=jasper_params["JasperEncoder"]["jasper"][-1]["filters"],num_classes=len(vocab))
    greedy_decoder = nemo_asr.GreedyCTCDecoder()

    if args.lm_path:
        beam_width = args.beam_width
        alpha = args.alpha
        beta = args.beta
        cutoff_prob = args.cutoff_prob
        cutoff_top_n = args.cutoff_top_n
        beam_search_with_lm = nemo_asr.BeamSearchDecoderWithLM(
            vocab=vocab,
            beam_width=beam_width,
            alpha=alpha,
            beta=beta,
            cutoff_prob=cutoff_prob,
            cutoff_top_n=cutoff_top_n,
            lm_path=args.lm_path,
            num_cpus=max(os.cpu_count(), 1))

    logger.info('================================')
    logger.info(f"Number of parameters in encoder: {jasper_encoder.num_weights}")
    logger.info(f"Number of parameters in decoder: {jasper_decoder.num_weights}")
    logger.info(f"Total number of parameters in decoder: "f"{jasper_decoder.num_weights + jasper_encoder.num_weights}")
    logger.info('================================')

    audio_signal_e1, a_sig_length_e1, transcript_e1, transcript_len_e1 = data_layer()
    processed_signal_e1, p_length_e1 = data_preprocessor(input_signal=audio_signal_e1, length=a_sig_length_e1)
    encoded_e1, encoded_len_e1 = jasper_encoder(audio_signal=processed_signal_e1, length=p_length_e1)
    log_probs_e1 = jasper_decoder(encoder_output=encoded_e1)
    predictions_e1 = greedy_decoder(log_probs=log_probs_e1)

    eval_tensors = [log_probs_e1, predictions_e1, transcript_e1, transcript_len_e1, encoded_len_e1]

    if args.lm_path:
        beam_predictions_e1 = beam_search_with_lm(log_probs=log_probs_e1, log_probs_length=encoded_len_e1)
        eval_tensors.append(beam_predictions_e1)

    evaluated_tensors = neural_factory.infer(tensors=eval_tensors, checkpoint_dir=load_dir,)

    greedy_hypotheses = post_process_predictions(evaluated_tensors[1], vocab)
    references = post_process_transcripts(evaluated_tensors[2], evaluated_tensors[3], vocab)
    cer = word_error_rate(hypotheses=greedy_hypotheses, references=references, use_cer=True)
    wer = word_error_rate(hypotheses=greedy_hypotheses, references=references, use_cer=False)
    logger.info("Greedy CER {:.2f}%".format(cer * 100))
    logger.info("Greedy WER {:.2f}%".format(wer * 100))

    if args.lm_path:
        beam_hypotheses = []
        # Over mini-batch
        for i in evaluated_tensors[-1]:
            # Over samples
            for j in i:
                beam_hypotheses.append(j[0][1])

        cer = word_error_rate(hypotheses=beam_hypotheses, references=references, use_cer=True)
        wer = word_error_rate(hypotheses=beam_hypotheses, references=references, use_cer=False)

        logger.info("Beam CER {:.2f}%".format(cer * 100))
        logger.info("Beam WER {:.2f}%".format(wer * 100))

    # if args.save_logprob:
    #     # Convert logits to list of numpy arrays
    #     logprob = []
    #     for i, batch in enumerate(evaluated_tensors[0]):
    #         for j in range(batch.shape[0]):
    #             logprob.append(
    #                 batch[j][:evaluated_tensors[4][i][j], :].cpu().numpy())
    #     with open(args.save_logprob, 'wb') as f:
    #         pickle.dump(logprob, f, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    main()