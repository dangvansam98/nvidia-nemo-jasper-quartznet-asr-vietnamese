import argparse
import copy
import os
import pickle
import torch
import numpy as np
from ruamel.yaml import YAML
import nemo
import nemo.collections.asr as nemo_asr
from nemo.collections.asr.helpers import post_process_predictions, post_process_transcripts, word_error_rate
logging = nemo.logging


def main():
    parser = argparse.ArgumentParser()
    # model params
    parser.add_argument("--model_config", default='config/quartznet12x1_abcfjwz.yaml', type=str, required=False)
    parser.add_argument("--eval_datasets",default='data/grapheme/vivos_test.json', type=str, required=False)
    parser.add_argument("--load_dir",default='quartznet12x1_abcfjz_them100h/checkpoints', type=str, required=False)
    # run params
    parser.add_argument("--local_rank", default=None, type=int)
    parser.add_argument("--batch_size", default=64, type=int)
    parser.add_argument("--amp_opt_level", default="O1", type=str)
    # store results
    parser.add_argument("--save_logprob", default=None, type=str)

    # lm inference parameters
    parser.add_argument("--lm_path", default='NeMo/scripts/language_model/4-gram-lm.binary', type=str)
    parser.add_argument('--alpha', default=2, type=float, help='value of LM weight', required=False)
    parser.add_argument('--alpha_max', default=3, type=float,help='maximum value of LM weight (for a grid search in eval mode)', required=False,)
    parser.add_argument('--alpha_step', type=float, help='step for LM weights tuning in eval mode', required=False, default=0.25)
    parser.add_argument('--beta', default=2.5, type=float, help='value of word count weight', required=False)
    parser.add_argument('--beta_max', type=float, default=2.5, help='maximum value of word count weight (for a grid search in eval mode')
    parser.add_argument('--beta_step', type=float, default=0.5, help='step for word count weights tuning in eval mode', required=False)
    parser.add_argument("--beam_width", default=1024, type=int)


    args = parser.parse_args()
    batch_size = args.batch_size
    load_dir = args.load_dir
    if args.local_rank is not None:
        if args.lm_path:
            raise NotImplementedError("Beam search decoder with LM does not currently support evaluation on multi-gpu.")
        device = nemo.core.DeviceType.AllGpu
    else:
        device = nemo.core.DeviceType.GPU

    # Instantiate Neural Factory with supported backend
    neural_factory = nemo.core.NeuralModuleFactory(
        backend=nemo.core.Backend.PyTorch,
        local_rank=args.local_rank,
        # optimization_level=args.amp_opt_level,
        placement=device,
    )

    if args.local_rank is not None:
        logging.info('Doing ALL GPU')

    yaml = YAML(typ="safe")
    with open(args.model_config) as f:
        jasper_params = yaml.load(f)
    vocab = jasper_params['labels']
    sample_rate = 16000#jasper_params['AudioToMelSpectrogramPreprocessor']['sample_rate']

    eval_datasets = "data/grapheme/vivos_test.json,data/grapheme/fpt_open_set001_test_clean.json"
    eval_datasets += ",data/grapheme/thaison_data_07012020_cuted.json,data/grapheme/thaison_data_07012020_cuted.json,data/grapheme/voip_audio_cuted_transcript.json"
    # eval_datasets = args.eval_datasets
    eval_dl_params = copy.deepcopy(jasper_params["AudioToTextDataLayer"])
    # eval_dl_params.update(jasper_params["AudioToTextDataLayer"]["eval"])
    # del eval_dl_params["train"]
    # del eval_dl_params["eval"]
    data_layer = nemo_asr.AudioToTextDataLayer(
        manifest_filepath=eval_datasets,
        sample_rate=sample_rate,
        labels=vocab,
        batch_size=batch_size,
        **eval_dl_params,
    )

    N = len(data_layer)
    logging.info('Evaluating {0} examples'.format(N))

    data_preprocessor = nemo_asr.AudioToMelSpectrogramPreprocessor(
        sample_rate=sample_rate, **jasper_params["AudioToMelSpectrogramPreprocessor"]
    )
    jasper_encoder = nemo_asr.JasperEncoder(
        feat_in=jasper_params["AudioToMelSpectrogramPreprocessor"]["features"], **jasper_params["JasperEncoder"]
    )
    jasper_decoder = nemo_asr.JasperDecoderForCTC(
        feat_in=jasper_params["JasperEncoder"]["jasper"][-1]["filters"], num_classes=len(vocab)
    )
    greedy_decoder = nemo_asr.GreedyCTCDecoder()

    logging.info('================================')
    logging.info(f"Number of parameters in encoder: {jasper_encoder.num_weights}")
    logging.info(f"Number of parameters in decoder: {jasper_decoder.num_weights}")
    logging.info(f"Total number of parameters in model: " f"{jasper_decoder.num_weights + jasper_encoder.num_weights}")
    logging.info('================================')

    # Define inference DAG
    audio_signal_e1, a_sig_length_e1, transcript_e1, transcript_len_e1 = data_layer()
    processed_signal_e1, p_length_e1 = data_preprocessor(input_signal=audio_signal_e1, length=a_sig_length_e1)
    encoded_e1, encoded_len_e1 = jasper_encoder(audio_signal=processed_signal_e1, length=p_length_e1)
    log_probs_e1 = jasper_decoder(encoder_output=encoded_e1)
    predictions_e1 = greedy_decoder(log_probs=log_probs_e1)

    eval_tensors = [log_probs_e1, predictions_e1, transcript_e1, transcript_len_e1, encoded_len_e1]

    # inference
    evaluated_tensors = neural_factory.infer(tensors=eval_tensors, checkpoint_dir=load_dir)
    #print('0:',evaluated_tensors[0][0][0].shape)
    #print('1:',evaluated_tensors[1][0][0])
    #print('2:',evaluated_tensors[2][0][0])
    greedy_hypotheses = post_process_predictions(evaluated_tensors[1], vocab)
    references = post_process_transcripts(evaluated_tensors[2], evaluated_tensors[3], vocab)
    #print('greedy_hypotheses:',greedy_hypotheses[3])
    #print('references:',references[3])
    #exit()
    wer = word_error_rate(hypotheses=greedy_hypotheses, references=references)
    cer = word_error_rate(hypotheses=greedy_hypotheses, references=references, use_cer=True)
    logging.info("Greedy CER {:.2f}%".format(cer * 100))
    logging.info("Greedy WER {:.2f}%".format(wer * 100))

    # Convert logits to list of numpy arrays
    logprob = []
    for i, batch in enumerate(evaluated_tensors[0]):
        for j in range(batch.shape[0]):
            logprob.append(batch[j][:int(evaluated_tensors[4][i][j]), :].cpu().numpy())
    # if args.save_logprob:
    #     with open(args.save_logprob, 'wb') as f:
    #         pickle.dump(logprob, f, protocol=pickle.HIGHEST_PROTOCOL)

    # language model
    if args.lm_path:
        if args.alpha_max is None:
            args.alpha_max = args.alpha
        # include alpha_max in tuning range
        args.alpha_max += args.alpha_step / 10.0

        if args.beta_max is None:
            args.beta_max = args.beta
        # include beta_max in tuning range
        args.beta_max += args.beta_step / 10.0

        beam_wers = []

        logprobexp = [np.exp(p) for p in logprob]
        # for beam_w in np.arange(100, 1200, 100):
        for alpha in np.arange(args.alpha, args.alpha_max, args.alpha_step):
            for beta in np.arange(args.beta, args.beta_max, args.beta_step):
                logging.info('================================')
                logging.info(f'Infering with (alpha, beta): ({alpha}, {beta})')
                for n_gram_lm in ["3-gram-lm","4-gram-lm","5-gram-lm","6-gram-lm"]:
                    beam_search_with_lm = nemo_asr.BeamSearchDecoderWithLM(
                        vocab=vocab,
                        beam_width=200,
                        alpha=alpha,
                        beta=beta,
                        cutoff_prob=0.99,
                        cutoff_top_n=40,
                        lm_path="NeMo/scripts/language_model2/{}.binary".format(n_gram_lm),
                        num_cpus=4,
                        input_tensor=False,
                    )

                    beam_predictions = beam_search_with_lm(log_probs=logprobexp, log_probs_length=None, force_pt=True)

                    beam_predictions = [b[0][1] for b in beam_predictions[0]]
                    lm_wer = word_error_rate(hypotheses=beam_predictions, references=references)
                    lm_cer = word_error_rate(hypotheses=beam_predictions, references=references, use_cer=True)
                    logging.info("Beam CER {:.2f}%".format(lm_cer * 100))
                    logging.info("Beam WER {:.2f}%".format(lm_wer * 100))
                    beam_wers.append(((alpha, beta, n_gram_lm), lm_wer * 100))

        logging.info('Beam WER for (alpha, beta, n_gram_lm)')
        logging.info('================================')
        logging.info('\n' + '\n'.join([str(e) for e in beam_wers]))
        logging.info('================================')
        best_beam_wer = min(beam_wers, key=lambda x: x[1])
        logging.info('Best (alpha, beta, n_gram_lm): ' f'{best_beam_wer[0]}, ' f'WER: {best_beam_wer[1]:.2f}%')


if __name__ == "__main__":
    main()