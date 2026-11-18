// Private stdio worker owned by the Windows-hosted Python process.
// It has no listening port and emits only bounded results and classified errors.
import readline from 'node:readline';
import { credentialsForProfile } from './aws_profile_credentials.mjs';
import {
  transcribeVegaAudioWithAws, transcribeVegaBufferedAudioWithAws, transcribeVegaPcmWithAws,
  synthesizeVegaSpeechWithAws, describeAwsVegaSpeechFailure,
} from './port/src/main/vega/awsSpeech.ts';
import { awsVoicesForModel, awsGenerativeRegionAvailable, AWS_GENERATIVE_REGIONS, speechModelLabel } from './port/src/shared/vegaVoices.ts';
import { expandCalendarEventOccurrences } from './port/src/main/vega/calendarRecurrence.ts';
import { buildVegaBriefing } from './port/src/renderer/vegaBriefing.ts';
import { spokenResponseText } from './port/src/shared/spokenText.ts';

const running = new Map();
const streams = new Map();
const cancelledCaptures = new Set();
const maxAudio = 12 * 1024 * 1024;
const output = (id, value) => process.stdout.write(JSON.stringify({ id, ...value }) + '\n');
function route(value) {
  if (!value || value.providerId !== 'aws'
      || value.transcriptionModelId !== 'amazon-transcribe-streaming'
      || !['en-US','fr-FR','it-IT','ar-SA','ar-AE','auto'].includes(value.transcriptionLanguage)
      || !['amazon-polly-neural','amazon-polly-generative'].includes(value.speechModelId)
      || !/^[a-z]{2}-[a-z]+-\d$/.test(value.awsRegion ?? '')
      || !/^[\w@.+-]{1,128}$/.test(value.awsProfile ?? '')
      || !awsVoicesForModel(value.speechModelId).includes(value.voice)) {
    throw new Error('The saved speech route is invalid.');
  }
  if (value.speechModelId === 'amazon-polly-generative' && !awsGenerativeRegionAvailable(value.awsRegion)) {
    throw new Error('The saved generative voice is unavailable in its selected region.');
  }
  return Object.freeze({ ...value, region: value.awsRegion });
}
class PcmQueue {
  chunks = []; queued = 0; total = 0; sequence = 0; ended = false; aborted = false; wake = null;
  push(sequence, bytes) {
    if (this.ended || this.aborted || sequence !== this.sequence
        || !Number.isSafeInteger(sequence) || bytes.length < 2 || bytes.length > 6400 || bytes.length % 2
        || this.queued + bytes.length > 320000 || this.total + bytes.length > 3840000) {
      this.abort(); throw new Error('The live audio stream is incomplete or exceeds its limit.');
    }
    this.sequence++; this.queued += bytes.length; this.total += bytes.length;
    this.chunks.push(Buffer.from(bytes)); this.wake?.();
  }
  finish(sequence) {
    if (this.aborted || sequence !== this.sequence) throw new Error('The complete live stream was not received.');
    this.ended = true; this.wake?.();
  }
  abort() { this.aborted = true; this.chunks = []; this.queued = 0; this.wake?.(); }
  async *[Symbol.asyncIterator]() {
    while (true) {
      if (this.aborted) throw new Error('Vega streaming audio was stopped.');
      const chunk = this.chunks.shift();
      if (chunk) { this.queued -= chunk.length; yield chunk; continue; }
      if (this.ended) return;
      await new Promise(resolve => { this.wake = resolve; });
      this.wake = null;
    }
  }
}
function audio(value) {
  if (typeof value !== 'string' || value.length > Math.ceil(maxAudio / 3) * 4
      || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new Error('The voice recording is invalid.');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.length < 4 || bytes.length > maxAudio) throw new Error('The voice recording exceeds its limit.');
  return bytes;
}
async function execute(message) {
  const { id, command, payload = {} } = message;
  if (typeof id !== 'string' || id.length > 100) return;
  if (command === 'cancel') {
    if (typeof payload.id === 'string') {
      cancelledCaptures.add(payload.id);
      if (cancelledCaptures.size > 512) cancelledCaptures.delete(cancelledCaptures.values().next().value);
    }
    running.get(payload.id)?.abort();
    const stream = streams.get(payload.id);
    if (stream) { stream.controller.abort(); stream.queue.abort(); clearTimeout(stream.timer); streams.delete(payload.id); }
    return output(id, { ok: true, value: { cancelled: true } });
  }
  let config, operation = command === 'synthesize' ? 'synthesis' : 'transcription';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 170000);
  running.set(id, controller);
  try {
    if (cancelledCaptures.has(id)) {
      controller.abort();
      throw Object.assign(new Error('The requested operation was stopped.'), { name: 'AbortError' });
    }
    let value;
    if (command === 'voice_catalog') {
      value = {
        providerId: 'aws', providerLabel: 'AWS Transcribe + Polly',
        transcriptionModelId: 'amazon-transcribe-streaming',
        languages: ['en-US','fr-FR','it-IT','ar-SA','ar-AE','auto'],
        speechModels: ['amazon-polly-neural','amazon-polly-generative'].map(modelId => ({
          modelId, label: speechModelLabel(modelId), voices: awsVoicesForModel(modelId),
          regions: modelId === 'amazon-polly-generative' ? AWS_GENERATIVE_REGIONS : null,
        })),
      };
    } else if (command === 'calendar') {
      const items = payload.events.flatMap(event => expandCalendarEventOccurrences(event, payload.from, payload.to, 400));
      value = items.slice(0, 1000);
    } else if (command === 'briefing') {
      value = buildVegaBriefing(payload);
    } else {
      config = route(message.config);
      if (command === 'validate_route') {
        value = { valid: true };
      } else if (command === 'probe') {
        const provider = credentialsForProfile(config.awsProfile);
        await provider();
        value = { configured: true, profile: config.awsProfile, region: config.region, credentialSource: provider.source() };
      } else if (command === 'synthesize') {
        if (typeof payload.text !== 'string' || !payload.text.trim() || payload.text.length > 16000) {
          throw new Error('Choose a reply of at most 16000 characters.');
        }
        const bytes = await synthesizeVegaSpeechWithAws(config, spokenResponseText(payload.text, 3000), 8 * 1024 * 1024, controller.signal);
        value = { audio: Buffer.from(bytes).toString('base64'), mime: 'audio/mpeg' };
      } else if (command === 'transcribe') {
        const transcribe = payload.buffered === true ? transcribeVegaBufferedAudioWithAws : transcribeVegaAudioWithAws;
        value = await transcribe(config, audio(payload.audio), controller.signal);
        if (value.text.length > 12000) throw new Error('The transcript exceeds the conversation limit.');
      } else if (command === 'live_begin') {
        if (streams.size || typeof payload.capture_id !== 'string' || cancelledCaptures.has(payload.capture_id)) throw new Error('This recording was cancelled or another live recording is active.');
        const queue = new PcmQueue();
        const liveController = new AbortController();
        const liveTimer = setTimeout(() => { liveController.abort(); queue.abort(); streams.delete(payload.capture_id); }, 170000);
        const result = transcribeVegaPcmWithAws(config, queue, liveController.signal)
          .then(value => ({ ok: true, value }), error => ({ ok: false, error: describeAwsVegaSpeechFailure(error, config, 'transcription') }));
        streams.set(payload.capture_id, { queue, controller: liveController, result, timer: liveTimer, route: JSON.stringify(config) });
        value = {};
      } else if (command === 'live_push' || command === 'live_finish') {
        const stream = streams.get(payload.capture_id);
        if (!stream || stream.route !== JSON.stringify(config)) throw new Error('The live recording or selected route changed.');
        if (command === 'live_push') {
          stream.queue.push(payload.sequence, Buffer.from(payload.audio, 'base64'));
          value = {};
        } else {
          try {
            stream.queue.finish(payload.sequence);
            const result = await stream.result;
            if (!result.ok) { output(id, result); return; }
            value = result.value;
          } finally {
            clearTimeout(stream.timer); streams.delete(payload.capture_id);
          }
        }
      } else {
        throw new Error('Unsupported speech command.');
      }
    }
    output(id, { ok: true, value });
  } catch (error) {
    const diagnostic = config
      ? describeAwsVegaSpeechFailure(error, config, operation, { cancelled: controller.signal.aborted })
      : { code: 'VEGA_SPEECH_CONFIGURATION', message: 'The saved speech route or worker request is invalid.' };
    output(id, { ok: false, error: diagnostic });
  } finally {
    clearTimeout(timer); running.delete(id);
  }
}
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', line => {
  if (line.length > 18 * 1024 * 1024) return;
  try {
    const message = JSON.parse(line);
    if (running.size >= 16 && message.command !== 'cancel') {
      output(message.id, { ok: false, error: { code: 'VEGA_SPEECH_BUSY', message: 'Voice is busy. Try again shortly.' } });
    } else {
      void execute(message);
    }
  } catch { /* malformed input carries no authority */ }
});
input.on('close', () => {
  for (const controller of running.values()) controller.abort();
  for (const stream of streams.values()) { stream.controller.abort(); stream.queue.abort(); clearTimeout(stream.timer); }
});
