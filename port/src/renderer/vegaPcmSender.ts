import type { ApiResult, VegaVoiceTranscript } from '../preload'

type Api = {
  beginVegaVoiceStream: (conversationId: string, captureId: string) => Promise<ApiResult<unknown>>
  pushVegaVoiceStream: (conversationId: string, captureId: string, sequence: number, chunk: Uint8Array) => Promise<ApiResult<unknown>>
  finishVegaVoiceStream: (conversationId: string, captureId: string, sequence: number) => Promise<ApiResult<VegaVoiceTranscript>>
  cancelVegaVoiceTranscription: (conversationId: string, captureId: string) => Promise<ApiResult<unknown>>
}
type Failure = { code: string; message: string }
async function bounded<T>(promise: Promise<T>, milliseconds: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([promise, new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('Voice transport timed out.')), milliseconds)
    })])
  } finally { clearTimeout(timer) }
}

/** Local pre-roll and ordered, bounded IPC. Starts a provider only after speech. */
export class VegaPcmSender {
  private readonly api: Api
  private readonly conversationId: string
  private readonly captureId: string
  private readonly onFailure: (error: Failure) => void
  private preRoll: Uint8Array[] = []
  private chain: Promise<void> = Promise.resolve()
  private sequence = 0
  private queued = 0
  private failure: Failure | null = null
  private cancelled = false
  private ending = false
  private didStart = false
  private completion: Promise<ApiResult<VegaVoiceTranscript>> | null = null
  constructor(api: Api, conversationId: string, captureId: string, onFailure: (error: Failure) => void) {
    this.api = api; this.conversationId = conversationId; this.captureId = captureId; this.onFailure = onFailure
  }
  get started(): boolean { return this.didStart }
  private fail(error: Failure): void {
    if (this.failure || this.cancelled) return
    this.failure = error
    this.preRoll = []
    void this.api.cancelVegaVoiceTranscription(this.conversationId, this.captureId).catch(() => undefined)
    this.onFailure(error)
  }
  frame(pcm: Uint8Array): void {
    if (this.cancelled || this.ending || this.failure) return
    if (!pcm.byteLength || pcm.byteLength > 6_400 || pcm.byteLength % 2) {
      this.fail({ code: 'VEGA_PCM_INVALID', message: 'Audio input returned an invalid frame.' }); return
    }
    const owned = Uint8Array.from(pcm)
    if (!this.didStart) {
      this.preRoll.push(owned)
      if (this.preRoll.length > 4) this.preRoll.shift()
      return
    }
    this.enqueue(owned)
  }
  start(): void {
    if (this.didStart || this.cancelled || this.failure || this.ending) return
    this.didStart = true
    this.chain = bounded(this.api.beginVegaVoiceStream(this.conversationId, this.captureId), 10_000)
      .then((result) => { if (!result.ok) this.fail(result.error) })
      .catch(() => this.fail({ code: 'VEGA_STREAM_START_FAILED', message: 'Live transcription could not start. Try again.' }))
    for (const chunk of this.preRoll) this.enqueue(chunk)
    this.preRoll = []
  }
  private enqueue(chunk: Uint8Array): void {
    if (this.queued >= 100) {
      this.fail({ code: 'VEGA_STREAM_SLOW', message: 'Audio could not be sent in time. Start Vega again.' }); return
    }
    this.queued += 1
    this.chain = this.chain.then(async () => {
      if (this.cancelled || this.failure) return
      try {
        const result = await bounded(this.api.pushVegaVoiceStream(this.conversationId, this.captureId, this.sequence++, chunk), 10_000)
        if (!result.ok) this.fail(result.error)
      } catch { this.fail({ code: 'VEGA_STREAM_SEND_FAILED', message: 'The voice connection was interrupted. Start Vega again.' }) }
    }).finally(() => { this.queued -= 1 })
  }
  finish(): Promise<ApiResult<VegaVoiceTranscript>> {
    if (this.completion) return this.completion
    this.ending = true
    this.completion = (async () => {
      await this.chain
      if (this.failure) return { ok: false as const, error: this.failure }
      if (this.cancelled || !this.didStart) return { ok: false as const, error: { code: 'VEGA_STREAM_CANCELLED', message: 'The voice turn ended.' } }
      try {
        return await bounded(this.api.finishVegaVoiceStream(this.conversationId, this.captureId, this.sequence), 160_000)
      } catch {
        this.cancel()
        return { ok: false as const, error: { code: 'VEGA_STREAM_FINISH_FAILED', message: 'Live transcription could not be completed. Try again.' } }
      }
    })()
    return this.completion
  }
  cancel(): void {
    if (this.cancelled) return
    this.cancelled = true
    this.preRoll = []
    void this.api.cancelVegaVoiceTranscription(this.conversationId, this.captureId).catch(() => undefined)
  }
}
