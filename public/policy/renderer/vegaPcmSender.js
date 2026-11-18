                                                                

            
                                                                                                  
                                                                                                                                      
                                                                                                                                 
                                                                                                          
 
                                                
async function bounded   (promise            , milliseconds        )             {
  let timer                                           
  try {
    return await Promise.race([promise, new Promise       ((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('Voice transport timed out.')), milliseconds)
    })])
  } finally { clearTimeout(timer) }
}

/** Local pre-roll and ordered, bounded IPC. Starts a provider only after speech. */
export class VegaPcmSender {
                   api     
                   conversationId        
                   captureId        
                   onFailure                          
          preRoll               = []
          chain                = Promise.resolve()
          sequence = 0
          queued = 0
          failure                 = null
          cancelled = false
          ending = false
          didStart = false
          completion                                                 = null
  constructor(api     , conversationId        , captureId        , onFailure                          ) {
    this.api = api; this.conversationId = conversationId; this.captureId = captureId; this.onFailure = onFailure
  }
  get started()          { return this.didStart }
          fail(error         )       {
    if (this.failure || this.cancelled) return
    this.failure = error
    this.preRoll = []
    void this.api.cancelVegaVoiceTranscription(this.conversationId, this.captureId).catch(() => undefined)
    this.onFailure(error)
  }
  frame(pcm            )       {
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
  start()       {
    if (this.didStart || this.cancelled || this.failure || this.ending) return
    this.didStart = true
    this.chain = bounded(this.api.beginVegaVoiceStream(this.conversationId, this.captureId), 10_000)
      .then((result) => { if (!result.ok) this.fail(result.error) })
      .catch(() => this.fail({ code: 'VEGA_STREAM_START_FAILED', message: 'Live transcription could not start. Try again.' }))
    for (const chunk of this.preRoll) this.enqueue(chunk)
    this.preRoll = []
  }
          enqueue(chunk            )       {
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
  finish()                                          {
    if (this.completion) return this.completion
    this.ending = true
    this.completion = (async () => {
      await this.chain
      if (this.failure) return { ok: false         , error: this.failure }
      if (this.cancelled || !this.didStart) return { ok: false         , error: { code: 'VEGA_STREAM_CANCELLED', message: 'The voice turn ended.' } }
      try {
        return await bounded(this.api.finishVegaVoiceStream(this.conversationId, this.captureId, this.sequence), 160_000)
      } catch {
        this.cancel()
        return { ok: false         , error: { code: 'VEGA_STREAM_FINISH_FAILED', message: 'Live transcription could not be completed. Try again.' } }
      }
    })()
    return this.completion
  }
  cancel()       {
    if (this.cancelled) return
    this.cancelled = true
    this.preRoll = []
    void this.api.cancelVegaVoiceTranscription(this.conversationId, this.captureId).catch(() => undefined)
  }
}
