from ruamel.yaml import YAML
# NeMo's "core" package
import nemo
# NeMo's ASR collection
import nemo.collections.asr as nemo_asr
from nemo.collections.asr.helpers import monitor_asr_train_progress, process_evaluation_batch, process_evaluation_epoch
from functools import partial


nf = nemo.core.NeuralModuleFactory(log_dir='QuartzNet12x1_all', create_tb_writer=True)
tb_writer = nf.tb_writer

#train_dataset = "data/vivos_test_simple.json,data/wavenet_new_simple.json,data/fpt_open_set001_train_clean_simple.json,data/voip_audio_cuted_transcript_simple.json,data/audio_18_11_cuted_transcript_simple.json"
train_dataset = "data/vivos_test_simple.json"
#eval_datasets = "data/vivos_test_simple.json,data/fpt_open_set001_test_clean_simple.json,data/to_tieng_transcript_simple.json"
eval_datasets = "data/vivos_test_simple.json, data/fpt_open_set001_test_clean_simple.json"

# QuartzNet Model definition
# Here we will be using separable convolutions
# with 12 blocks (k=12 repeated once r=1 from the picture above)
yaml = YAML(typ="safe")
with open("config/quartznet12x1.yaml") as f:
    quartznet_model_definition = yaml.load(f)

labels = quartznet_model_definition['labels']
print(len(labels), labels)
# Instantiate neural modules
data_layer = nemo_asr.AudioToTextDataLayer(manifest_filepath=train_dataset, labels=labels, batch_size=8)
data_layer_val = nemo_asr.AudioToTextDataLayer(manifest_filepath=eval_datasets, labels=labels, batch_size=8, shuffle=False)

data_preprocessor = nemo_asr.AudioToMelSpectrogramPreprocessor()
spec_augment = nemo_asr.SpectrogramAugmentation(rect_masks=5)

encoder = nemo_asr.JasperEncoder(feat_in=64, **quartznet_model_definition['JasperEncoder'])
decoder = nemo_asr.JasperDecoderForCTC(feat_in=1024, num_classes=len(labels))
ctc_loss = nemo_asr.CTCLossNM(num_classes=len(labels))
greedy_decoder = nemo_asr.GreedyCTCDecoder()

#CHECKPOINT_ENCODER = 'QuartzNet12x1_vivos/checkpoints/JasperEncoder-STEP-36700.pt'
#CHECKPOINT_DECODER = 'QuartzNet12x1_vivos/checkpoints/JasperDecoderForCTC-STEP-36700.pt'

#encoder.restore_from(CHECKPOINT_ENCODER)
#decoder.restore_from(CHECKPOINT_DECODER)

audio_signal, audio_signal_len, transcript, transcript_len = data_layer()
processed_signal, processed_signal_len = data_preprocessor(input_signal=audio_signal, length=audio_signal_len)

# Data argument
aug_signal = spec_augment(input_spec=processed_signal)
encoded, encoded_len = encoder(audio_signal=aug_signal, length=processed_signal_len)
log_probs = decoder(encoder_output=encoded)
predictions = greedy_decoder(log_probs=log_probs)
loss = ctc_loss(log_probs=log_probs, targets=transcript, input_length=encoded_len, target_length=transcript_len)

audio_signal_v, audio_signal_len_v, transcript_v, transcript_len_v = data_layer_val()
processed_signal_v, processed_signal_len_v = data_preprocessor(input_signal=audio_signal_v, length=audio_signal_len_v)
# Note that we are not using data-augmentation in validation DAG
encoded_v, encoded_len_v = encoder(audio_signal=processed_signal_v, length=processed_signal_len_v)
log_probs_v = decoder(encoder_output=encoded_v)
predictions_v = greedy_decoder(log_probs=log_probs_v)
loss_v = ctc_loss(log_probs=log_probs_v, targets=transcript_v, input_length=encoded_len_v, target_length=transcript_len_v)

train_callback = nemo.core.SimpleLossLoggerCallback(
    tb_writer=tb_writer,
    tensors=[loss, predictions, transcript, transcript_len],
    print_func=partial(monitor_asr_train_progress, labels=labels
    ))

saver_callback = nemo.core.CheckpointCallback(
    folder="QuartzNet12x1_all/checkpoints",# load_from_folder="QuartzNet12x1_vivos/checkpoints", 
    step_freq=1000, checkpoints_to_keep=2)

eval_callback = nemo.core.EvaluatorCallback(
    eval_tensors=[loss_v, predictions_v, transcript_v, transcript_len_v],
    user_iter_callback=partial(process_evaluation_batch, labels=labels),
    user_epochs_done_callback=partial(process_evaluation_epoch, tag="valid"),
    eval_step=500,
    tb_writer=tb_writer)

nf.train(
    # Specify the loss to optimize for
    tensors_to_optimize=[loss],
    callbacks=[train_callback, eval_callback, saver_callback],
    optimizer="novograd",
    optimization_params={ "num_epochs": 150, "lr": 0.01, "weight_decay": 1e-4 }
    )