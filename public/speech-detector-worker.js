import * as ort from './vendor/onnxruntime/ort.wasm.min.mjs';
import { SileroStream } from './silero-stream.js';

// Assets stay on the loopback origin. One WASM thread, no GPU or remote model.
ort.env.wasm.numThreads = 1;
ort.env.wasm.proxy = false;
ort.env.wasm.wasmPaths = new URL('./vendor/onnxruntime/', import.meta.url).href;
ort.env.logLevel = 'error';

let detector = null, nextSequence = 0, pending = 0, stopped = false;
let queue = Promise.resolve();
const fail = () => {
  if (stopped) return;
  stopped = true;
  self.postMessage({ type: 'error', message: 'Speech detection stopped. Resume listening to reconnect.' });
};
async function initialize() {
  const response = await fetch(new URL('./vendor/silero/silero.onnx', import.meta.url));
  if (!response.ok) throw new Error('Speech model unavailable.');
  const bytes = await response.arrayBuffer();
  const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
    .map(value => value.toString(16).padStart(2, '0')).join('');
  if (digest !== '7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49') {
    throw new Error('Speech model changed.');
  }
  const session = await ort.InferenceSession.create(bytes, {
    executionProviders: ['wasm'], executionMode: 'sequential',
  });
  detector = new SileroStream({ session, Tensor: ort.Tensor });
  self.postMessage({ type: 'ready', detector: 'silero' });
}
self.onmessage = ({ data }) => {
  if (stopped) return;
  if (!detector || data?.type !== 'frame' || data.sequence !== nextSequence++
      || !(data.pcm instanceof ArrayBuffer) || ++pending > 12) { fail(); return; }
  queue = queue.then(async () => {
    if (stopped) return;
    const probability = await detector.process(new Uint8Array(data.pcm));
    --pending;
    if (!stopped) self.postMessage({ type: 'frame', sequence: data.sequence,
      pcm: data.pcm, speechProbability: probability }, [data.pcm]);
  }).catch(fail);
};
void initialize().catch(fail);
