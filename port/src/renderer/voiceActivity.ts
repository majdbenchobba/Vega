export type VoiceActivityConfig = Readonly<{
  speechThreshold: number
  silenceThreshold: number
  speechOnsetMs: number
  endSilenceMs: number
  maxDurationMs: number
}>

export type VoiceActivitySample = Readonly<{
  timestampMs: number
  level: number
}>

export type VoiceActivityPhase =
  | 'waiting'
  | 'onset'
  | 'speech'
  | 'silence'
  | 'ended'

export type VoiceActivityCompletion =
  | 'silence'
  | 'max-duration'
  | 'no-speech'

export type VoiceActivityState = Readonly<{
  phase: VoiceActivityPhase
  captureStartedAtMs: number | null
  lastTimestampMs: number | null
  onsetStartedAtMs: number | null
  speechStartedAtMs: number | null
  silenceStartedAtMs: number | null
  completion: VoiceActivityCompletion | null
}>

export type VoiceActivityEvent =
  | Readonly<{
      type: 'speech-start'
      timestampMs: number
    }>
  | Readonly<{
      type: 'end-of-utterance'
      timestampMs: number
      reason: 'silence' | 'max-duration'
    }>
  | Readonly<{
      type: 'no-speech-timeout'
      timestampMs: number
    }>

export type VoiceActivityTransition = Readonly<{
  state: VoiceActivityState
  events: readonly VoiceActivityEvent[]
}>

export const DEFAULT_VOICE_ACTIVITY_CONFIG: VoiceActivityConfig = {
  speechThreshold: 0.55,
  silenceThreshold: 0.35,
  speechOnsetMs: 180,
  endSilenceMs: 900,
  maxDurationMs: 30_000
}

const NO_EVENTS: readonly VoiceActivityEvent[] = []

export function createVoiceActivityState(): VoiceActivityState {
  return {
    phase: 'waiting',
    captureStartedAtMs: null,
    lastTimestampMs: null,
    onsetStartedAtMs: null,
    speechStartedAtMs: null,
    silenceStartedAtMs: null,
    completion: null
  }
}

export function resetVoiceActivity(): VoiceActivityState {
  return createVoiceActivityState()
}

function assertLevel(name: string, value: number): void {
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    throw new RangeError(`${name} must be between 0 and 1.`)
  }
}

function assertDuration(name: string, value: number): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new RangeError(`${name} must be greater than zero.`)
  }
}

function assertConfig(config: VoiceActivityConfig): void {
  assertLevel('speechThreshold', config.speechThreshold)
  assertLevel('silenceThreshold', config.silenceThreshold)
  if (config.silenceThreshold > config.speechThreshold) {
    throw new RangeError(
      'silenceThreshold must not exceed speechThreshold.'
    )
  }
  assertDuration('speechOnsetMs', config.speechOnsetMs)
  assertDuration('endSilenceMs', config.endSilenceMs)
  assertDuration('maxDurationMs', config.maxDurationMs)
}

function assertSample(
  state: VoiceActivityState,
  sample: VoiceActivitySample
): void {
  if (!Number.isFinite(sample.timestampMs) || sample.timestampMs < 0) {
    throw new RangeError('timestampMs must be a non-negative number.')
  }
  assertLevel('level', sample.level)
  if (
    state.lastTimestampMs !== null &&
    sample.timestampMs < state.lastTimestampMs
  ) {
    throw new RangeError('Voice activity samples must be chronological.')
  }
}

function endedState(
  state: VoiceActivityState,
  timestampMs: number,
  completion: VoiceActivityCompletion
): VoiceActivityState {
  return {
    ...state,
    phase: 'ended',
    lastTimestampMs: timestampMs,
    onsetStartedAtMs: null,
    silenceStartedAtMs: null,
    completion
  }
}

export function reduceVoiceActivity(
  state: VoiceActivityState,
  sample: VoiceActivitySample,
  config: VoiceActivityConfig = DEFAULT_VOICE_ACTIVITY_CONFIG
): VoiceActivityTransition {
  assertConfig(config)
  assertSample(state, sample)
  if (state.phase === 'ended') {
    return { state, events: NO_EVENTS }
  }

  const timestampMs = sample.timestampMs
  const captureStartedAtMs = state.captureStartedAtMs ?? timestampMs
  const events: VoiceActivityEvent[] = []
  let next: VoiceActivityState = {
    ...state,
    captureStartedAtMs,
    lastTimestampMs: timestampMs
  }

  if (state.phase === 'waiting') {
    if (sample.level >= config.speechThreshold) {
      next = {
        ...next,
        phase: 'onset',
        onsetStartedAtMs: timestampMs
      }
    }
  } else if (state.phase === 'onset') {
    if (sample.level < config.speechThreshold) {
      next = {
        ...next,
        phase: 'waiting',
        onsetStartedAtMs: null
      }
    } else if (
      state.onsetStartedAtMs !== null &&
      timestampMs - state.onsetStartedAtMs >= config.speechOnsetMs
    ) {
      next = {
        ...next,
        phase: 'speech',
        onsetStartedAtMs: null,
        speechStartedAtMs: state.onsetStartedAtMs
      }
      events.push({ type: 'speech-start', timestampMs })
    }
  } else if (state.phase === 'speech') {
    if (sample.level <= config.silenceThreshold) {
      next = {
        ...next,
        phase: 'silence',
        silenceStartedAtMs: timestampMs
      }
    }
  } else if (sample.level > config.silenceThreshold) {
    next = {
      ...next,
      phase: 'speech',
      silenceStartedAtMs: null
    }
  } else if (
    state.silenceStartedAtMs !== null &&
    timestampMs - state.silenceStartedAtMs >= config.endSilenceMs
  ) {
    next = endedState(next, timestampMs, 'silence')
    events.push({
      type: 'end-of-utterance',
      timestampMs,
      reason: 'silence'
    })
  }

  if (
    next.phase !== 'ended' &&
    timestampMs - captureStartedAtMs >= config.maxDurationMs
  ) {
    if (next.speechStartedAtMs === null) {
      next = endedState(next, timestampMs, 'no-speech')
      events.push({ type: 'no-speech-timeout', timestampMs })
    } else {
      next = endedState(next, timestampMs, 'max-duration')
      events.push({
        type: 'end-of-utterance',
        timestampMs,
        reason: 'max-duration'
      })
    }
  }

  return { state: next, events }
}
