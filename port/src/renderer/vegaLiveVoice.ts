export type VegaLiveVoicePhase =
  | 'idle'
  | 'listening'
  | 'transcribing'
  | 'sending'
  | 'waiting'
  | 'speaking'

export type VegaLiveVoiceOperationId = `vega-live-${number}`

export type VegaLiveVoiceTarget =
  | Readonly<{ kind: 'conversation'; conversationId: string }>
  | Readonly<{ kind: 'detached-chat'; detachedChatId: string }>
  | Readonly<{ kind: 'project'; projectId: string }>
  | Readonly<{ kind: 'task'; taskId: string }>
  | Readonly<{ kind: 'app-view'; viewId: string }>

export type VegaLiveVoiceContextSnapshot = Readonly<{
  revision: number
  target: VegaLiveVoiceTarget
}>

export type VegaLiveVoiceFailure = Readonly<{
  code: string
  message: string
}>

export type VegaLiveVoiceEndReason =
  | Readonly<{ kind: 'stopped' }>
  | Readonly<{ kind: 'error'; failure: VegaLiveVoiceFailure }>
  | Readonly<{ kind: 'authorization-lost'; message: string }>
  | Readonly<{ kind: 'session-lost'; message: string }>

type VegaLiveVoiceSequence = Readonly<{
  nextOperationSequence: number
  context: VegaLiveVoiceContextSnapshot | null
}>

type VegaLiveVoiceActive = Readonly<{
  nextOperationSequence: number
  context: VegaLiveVoiceContextSnapshot
  sessionId: string
  operationId: VegaLiveVoiceOperationId
  turnContext: VegaLiveVoiceContextSnapshot
}>

export type VegaLiveVoiceState =
  | (VegaLiveVoiceSequence & Readonly<{
      phase: 'idle'
      endReason: VegaLiveVoiceEndReason | null
    }>)
  | (VegaLiveVoiceActive & Readonly<{
      phase: 'listening'
      recoverableFailure: VegaLiveVoiceFailure | null
    }>)
  | (VegaLiveVoiceActive & Readonly<{ phase: 'transcribing' }>)
  | (VegaLiveVoiceActive & Readonly<{ phase: 'sending' }>)
  | (VegaLiveVoiceActive & Readonly<{ phase: 'waiting' }>)
  | (VegaLiveVoiceActive & Readonly<{ phase: 'speaking' }>)

export type VegaLiveVoiceCleanupReason =
  | 'stop'
  | 'barge-in'
  | 'context-changed'
  | 'error'
  | 'authorization-lost'
  | 'session-lost'

type VegaLiveVoiceEffectContext = Readonly<{
  sessionId: string
  context: VegaLiveVoiceContextSnapshot
}>

export type VegaLiveVoiceEffect =
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'capture-start'
      operationId: VegaLiveVoiceOperationId
    }>)
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'capture-stop'
      operationId: VegaLiveVoiceOperationId
      discard: boolean
      reason: 'silence' | VegaLiveVoiceCleanupReason
    }>)
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'transcribe'
      operationId: VegaLiveVoiceOperationId
      captureOperationId: VegaLiveVoiceOperationId
    }>)
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'transcription-cancel'
      operationId: VegaLiveVoiceOperationId
      reason: VegaLiveVoiceCleanupReason
    }>)
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'send'
      operationId: VegaLiveVoiceOperationId
      text: string
    }>)
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'response-cancel'
      operationId: VegaLiveVoiceOperationId
      reason: VegaLiveVoiceCleanupReason
    }>)
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'speak'
      operationId: VegaLiveVoiceOperationId
      text: string
    }>)
  | (VegaLiveVoiceEffectContext & Readonly<{
      type: 'speech-stop'
      operationId: VegaLiveVoiceOperationId
      reason: VegaLiveVoiceCleanupReason
    }>)

type VegaLiveVoiceOperationEvent = Readonly<{
  sessionId: string
  operationId: VegaLiveVoiceOperationId
}>

export type VegaLiveVoiceEvent =
  | Readonly<{
      type: 'start'
      sessionId: string
    }>
  | Readonly<{
      type: 'context-changed'
      context: VegaLiveVoiceContextSnapshot
    }>
  | (VegaLiveVoiceOperationEvent & Readonly<{ type: 'silence-end' }>)
  | (VegaLiveVoiceOperationEvent & Readonly<{
      type: 'transcription-complete'
      text: string
    }>)
  | (VegaLiveVoiceOperationEvent & Readonly<{ type: 'send-accepted' }>)
  | (VegaLiveVoiceOperationEvent & Readonly<{
      type: 'reply-ready'
      text: string
    }>)
  | (VegaLiveVoiceOperationEvent & Readonly<{ type: 'speech-complete' }>)
  | Readonly<{ type: 'stop' }>
  | (VegaLiveVoiceOperationEvent & Readonly<{ type: 'barge-in' }>)
  | (VegaLiveVoiceOperationEvent & Readonly<{
      type: 'operation-failed'
      failure: VegaLiveVoiceFailure
      recovery: 'listen' | 'stop'
    }>)
  | Readonly<{
      type: 'authorization-lost'
      sessionId: string
      message?: string
    }>
  | Readonly<{
      type: 'session-lost'
      sessionId: string
      message?: string
    }>

export type VegaLiveVoiceTransition = Readonly<{
  state: VegaLiveVoiceState
  effects: readonly VegaLiveVoiceEffect[]
}>

type ActiveVegaLiveVoiceState = Exclude<
  VegaLiveVoiceState,
  { phase: 'idle' }
>

const NO_EFFECTS: readonly VegaLiveVoiceEffect[] = []

export function createVegaLiveVoiceState(
  context: VegaLiveVoiceContextSnapshot | null = null
): VegaLiveVoiceState {
  return {
    phase: 'idle',
    nextOperationSequence: 1,
    context: validContextSnapshot(context) ? context : null,
    endReason: null
  }
}

export function isVegaLiveVoiceActive(
  state: VegaLiveVoiceState
): state is ActiveVegaLiveVoiceState {
  return state.phase !== 'idle'
}

function unchanged(state: VegaLiveVoiceState): VegaLiveVoiceTransition {
  return { state, effects: NO_EFFECTS }
}

function transition(
  state: VegaLiveVoiceState,
  effects: readonly VegaLiveVoiceEffect[] = NO_EFFECTS
): VegaLiveVoiceTransition {
  return { state, effects }
}

function allocateOperation(
  state: VegaLiveVoiceSequence
): Readonly<{
  operationId: VegaLiveVoiceOperationId
  nextOperationSequence: number
}> {
  const sequence = state.nextOperationSequence
  return {
    operationId: `vega-live-${sequence}`,
    nextOperationSequence: sequence + 1
  }
}

function validId(value: string): boolean {
  return value.trim().length > 0
}

function validContextSnapshot(
  context: VegaLiveVoiceContextSnapshot | null
): context is VegaLiveVoiceContextSnapshot {
  if (
    !context ||
    !Number.isSafeInteger(context.revision) ||
    context.revision < 0
  ) return false

  const target = context.target
  if (target.kind === 'conversation') return validId(target.conversationId)
  if (target.kind === 'detached-chat') return validId(target.detachedChatId)
  if (target.kind === 'project') return validId(target.projectId)
  if (target.kind === 'task') return validId(target.taskId)
  return target.kind === 'app-view' && validId(target.viewId)
}

function newerContext(
  current: VegaLiveVoiceContextSnapshot | null,
  candidate: VegaLiveVoiceContextSnapshot
): boolean {
  return validContextSnapshot(candidate) && (
    current === null ||
    candidate.revision > current.revision
  )
}

function normalizedFailure(
  failure: VegaLiveVoiceFailure
): VegaLiveVoiceFailure {
  return {
    code: failure.code.trim() || 'VEGA_LIVE_VOICE_FAILED',
    message: failure.message.trim() || 'Vega live voice stopped after an operation failed.'
  }
}

function listeningState(
  state: VegaLiveVoiceSequence,
  sessionId: string,
  context: VegaLiveVoiceContextSnapshot,
  recoverableFailure: VegaLiveVoiceFailure | null
): Readonly<{
  state: Extract<VegaLiveVoiceState, { phase: 'listening' }>
  effect: Extract<VegaLiveVoiceEffect, { type: 'capture-start' }>
}> {
  const allocated = allocateOperation(state)
  return {
    state: {
      phase: 'listening',
      sessionId,
      context,
      turnContext: context,
      ...allocated,
      recoverableFailure
    },
    effect: {
      type: 'capture-start',
      sessionId,
      context,
      operationId: allocated.operationId
    }
  }
}

function idleState(
  state: ActiveVegaLiveVoiceState,
  endReason: VegaLiveVoiceEndReason
): Extract<VegaLiveVoiceState, { phase: 'idle' }> {
  return {
    phase: 'idle',
    nextOperationSequence: state.nextOperationSequence,
    context: state.context,
    endReason
  }
}

function operationMatches(
  state: VegaLiveVoiceState,
  event: VegaLiveVoiceOperationEvent
): state is ActiveVegaLiveVoiceState {
  return (
    isVegaLiveVoiceActive(state) &&
    state.sessionId === event.sessionId &&
    state.operationId === event.operationId
  )
}

function contextOf(
  state: ActiveVegaLiveVoiceState
): VegaLiveVoiceEffectContext {
  return {
    sessionId: state.sessionId,
    context: state.turnContext
  }
}

function cleanupEffects(
  state: ActiveVegaLiveVoiceState,
  reason: VegaLiveVoiceCleanupReason
): readonly VegaLiveVoiceEffect[] {
  const context = contextOf(state)
  if (state.phase === 'listening') {
    return [{
      type: 'capture-stop',
      ...context,
      operationId: state.operationId,
      discard: true,
      reason
    }]
  }
  if (state.phase === 'transcribing') {
    return [{
      type: 'transcription-cancel',
      ...context,
      operationId: state.operationId,
      reason
    }]
  }
  if (state.phase === 'sending' || state.phase === 'waiting') {
    return [{
      type: 'response-cancel',
      ...context,
      operationId: state.operationId,
      reason
    }]
  }
  return [{
    type: 'speech-stop',
    ...context,
    operationId: state.operationId,
    reason
  }]
}

function resumeListening(
  state: ActiveVegaLiveVoiceState,
  recoverableFailure: VegaLiveVoiceFailure | null,
  cleanupReason?: VegaLiveVoiceCleanupReason
): VegaLiveVoiceTransition {
  const listening = listeningState(
    state,
    state.sessionId,
    state.context,
    recoverableFailure
  )
  return transition(
    listening.state,
    cleanupReason
      ? [...cleanupEffects(state, cleanupReason), listening.effect]
      : [listening.effect]
  )
}

export function reduceVegaLiveVoice(
  state: VegaLiveVoiceState,
  event: VegaLiveVoiceEvent
): VegaLiveVoiceTransition {
  if (event.type === 'context-changed') {
    if (!newerContext(state.context, event.context)) return unchanged(state)
    if (state.phase === 'idle') {
      return transition({
        ...state,
        context: event.context
      })
    }
    if (state.phase !== 'listening') {
      return transition({
        ...state,
        context: event.context
      })
    }
    const listening = listeningState(
      state,
      state.sessionId,
      event.context,
      state.recoverableFailure
    )
    return transition(
      listening.state,
      [
        ...cleanupEffects(state, 'context-changed'),
        listening.effect
      ]
    )
  }

  if (event.type === 'start') {
    if (state.phase !== 'idle') return unchanged(state)
    if (!validId(event.sessionId)) {
      return transition({
        phase: 'idle',
        nextOperationSequence: state.nextOperationSequence,
        context: state.context,
        endReason: {
          kind: 'error',
          failure: {
            code: 'VEGA_LIVE_VOICE_SESSION_INVALID',
            message: 'Vega live voice requires an active session.'
          }
        }
      })
    }
    if (!state.context) {
      return transition({
        phase: 'idle',
        nextOperationSequence: state.nextOperationSequence,
        context: null,
        endReason: {
          kind: 'error',
          failure: {
            code: 'VEGA_LIVE_VOICE_CONTEXT_REQUIRED',
            message: 'Vega live voice requires a current app context.'
          }
        }
      })
    }
    const listening = listeningState(
      state,
      event.sessionId,
      state.context,
      null
    )
    return transition(listening.state, [listening.effect])
  }

  if (event.type === 'stop') {
    if (!isVegaLiveVoiceActive(state)) return unchanged(state)
    return transition(
      idleState(state, { kind: 'stopped' }),
      cleanupEffects(state, 'stop')
    )
  }

  if (event.type === 'authorization-lost') {
    if (!isVegaLiveVoiceActive(state) || state.sessionId !== event.sessionId) {
      return unchanged(state)
    }
    return transition(
      idleState(state, {
        kind: 'authorization-lost',
        message: event.message?.trim() || 'Vega authorization is no longer available.'
      }),
      cleanupEffects(state, 'authorization-lost')
    )
  }

  if (event.type === 'session-lost') {
    if (!isVegaLiveVoiceActive(state) || state.sessionId !== event.sessionId) {
      return unchanged(state)
    }
    return transition(
      idleState(state, {
        kind: 'session-lost',
        message: event.message?.trim() || 'The Vega live voice session ended.'
      }),
      cleanupEffects(state, 'session-lost')
    )
  }

  if (!operationMatches(state, event)) return unchanged(state)

  if (event.type === 'barge-in') {
    if (state.phase === 'listening') return unchanged(state)
    return resumeListening(state, null, 'barge-in')
  }

  if (event.type === 'operation-failed') {
    const failure = normalizedFailure(event.failure)
    if (event.recovery === 'listen') {
      return resumeListening(state, failure, 'error')
    }
    return transition(
      idleState(state, { kind: 'error', failure }),
      cleanupEffects(state, 'error')
    )
  }

  if (event.type === 'silence-end') {
    if (state.phase !== 'listening') return unchanged(state)
    const allocated = allocateOperation(state)
    const effectContext = contextOf(state)
    return transition(
      {
        phase: 'transcribing',
        sessionId: state.sessionId,
        context: state.context,
        turnContext: state.turnContext,
        ...allocated
      },
      [
        {
          type: 'capture-stop',
          ...effectContext,
          operationId: state.operationId,
          discard: false,
          reason: 'silence'
        },
        {
          type: 'transcribe',
          ...effectContext,
          operationId: allocated.operationId,
          captureOperationId: state.operationId
        }
      ]
    )
  }

  if (event.type === 'transcription-complete') {
    if (state.phase !== 'transcribing') return unchanged(state)
    const text = event.text.trim()
    if (!text) {
      return resumeListening(state, {
        code: 'VEGA_LIVE_VOICE_EMPTY_TRANSCRIPT',
        message: 'No speech was detected.'
      })
    }
    const allocated = allocateOperation(state)
    const effectContext = contextOf(state)
    return transition(
      {
        phase: 'sending',
        sessionId: state.sessionId,
        context: state.context,
        turnContext: state.turnContext,
        ...allocated
      },
      [{
        type: 'send',
        ...effectContext,
        operationId: allocated.operationId,
        text
      }]
    )
  }

  if (event.type === 'send-accepted') {
    if (state.phase !== 'sending') return unchanged(state)
    return transition({
      ...state,
      phase: 'waiting'
    })
  }

  if (event.type === 'reply-ready') {
    if (state.phase !== 'waiting') return unchanged(state)
    const text = event.text.trim()
    if (!text) {
      return resumeListening(state, {
        code: 'VEGA_LIVE_VOICE_EMPTY_REPLY',
        message: 'Vega returned no spoken reply.'
      })
    }
    const allocated = allocateOperation(state)
    const effectContext = contextOf(state)
    return transition(
      {
        phase: 'speaking',
        sessionId: state.sessionId,
        context: state.context,
        turnContext: state.turnContext,
        ...allocated
      },
      [{
        type: 'speak',
        ...effectContext,
        operationId: allocated.operationId,
        text
      }]
    )
  }

  if (event.type === 'speech-complete') {
    if (state.phase !== 'speaking') return unchanged(state)
    return resumeListening(state, null)
  }

  return unchanged(state)
}
