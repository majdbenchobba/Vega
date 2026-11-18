import type { ChatTurnResult, Message, VegaSpeechConfig } from '../preload'

export type VegaVoiceDraftOrigin = {
  conversationId: string
  draftText: string
  playbackRouteKey: string
  authorizationGeneration: number
}

export type VegaVoiceSendReceipt = {
  conversationId: string
  runId: string
  playbackRouteKey: string
  authorizationGeneration: number
}

export function vegaPlaybackRouteKey(
  config: VegaSpeechConfig | null
): string | null {
  if (!config?.configured) return null
  return JSON.stringify([
    config.providerId,
    config.speechModelId,
    config.voice,
    config.awsRegion,
    config.awsProfile
  ])
}

export function createVegaVoiceSendReceipt(
  origin: VegaVoiceDraftOrigin | null,
  conversationId: string,
  submittedDraft: string,
  runId: string
): VegaVoiceSendReceipt | null {
  if (
    !origin ||
    origin.conversationId !== conversationId ||
    origin.draftText !== submittedDraft
  ) return null
  return {
    conversationId,
    runId,
    playbackRouteKey: origin.playbackRouteKey,
    authorizationGeneration: origin.authorizationGeneration
  }
}

export function completedVegaVoiceReply(
  receipt: VegaVoiceSendReceipt | null,
  result: Pick<ChatTurnResult, 'conversationId' | 'runId' | 'assistantMessage'>,
  activeConversationId: string | null,
  playbackRouteKey: string | null,
  authorizationGeneration: number,
  autoSpeak: boolean
): Message | null {
  if (
    !receipt ||
    !autoSpeak ||
    receipt.conversationId !== activeConversationId ||
    receipt.conversationId !== result.conversationId ||
    receipt.runId !== result.runId ||
    receipt.playbackRouteKey !== playbackRouteKey ||
    receipt.authorizationGeneration !== authorizationGeneration
  ) return null
  const reply = result.assistantMessage
  return reply?.role === 'assistant' && reply.conversation_id === receipt.conversationId
    ? reply
    : null
}
