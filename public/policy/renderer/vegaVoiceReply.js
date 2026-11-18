                                                                           

                                    
                        
                   
                          
                                 
 

                                    
                        
               
                          
                                 
 

export function vegaPlaybackRouteKey(
  config                         
)                {
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
  origin                             ,
  conversationId        ,
  submittedDraft        ,
  runId        
)                              {
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
  receipt                             ,
  result                                                                       ,
  activeConversationId               ,
  playbackRouteKey               ,
  authorizationGeneration        ,
  autoSpeak         
)                 {
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
