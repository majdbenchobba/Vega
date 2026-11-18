export const VEGA_BRIEFING_SIGNAL_KEY_LIMIT = 320
export const VEGA_BRIEFING_SIGNAL_REVISION_LIMIT = 160

                                  
                   
                  
 

                                                                
                        
 

                                                                       
                                             
 

const VEGA_BRIEFING_SIGNAL_KEY_PATTERN =
  /^(approvals|queue|conversation-summary|project-summary|conversation-tasks|project-tasks|failure|desktop-update|reminder|calendar):[A-Za-z0-9._:-]+$/

export function validVegaBriefingSignal(signal                    )          {
  return (
    signal.signalKey.length > 0 &&
    signal.signalKey.length <= VEGA_BRIEFING_SIGNAL_KEY_LIMIT &&
    VEGA_BRIEFING_SIGNAL_KEY_PATTERN.test(signal.signalKey) &&
    signal.revision.length > 0 &&
    signal.revision.length <= VEGA_BRIEFING_SIGNAL_REVISION_LIMIT &&
    !signal.revision.includes('\0')
  )
}

export function validVegaBriefingRevision(revision        )          {
  return (
    revision.length > 0 &&
    revision.length <= VEGA_BRIEFING_SIGNAL_REVISION_LIMIT &&
    !revision.includes('\0')
  )
}
