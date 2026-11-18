                               
                 
          
               
                  
              
            
              
               

                                       
           
               
         
                 

                                
           
               
                
            
                
               

                                       
          
               
                    
               

export const VEGA_PRESENCE_SCOPE_LABEL = 'App-wide voice'
export const VEGA_PRESENCE_SCOPE_DESCRIPTION =
  'Vega stays available across conversations, projects, tasks, and views. Listening follows your automatic-start preference or an explicit Start. Active sessions continue in the background; use Pause to stop.'

                                                 
                          
                    
                          
                            
                                          
                      
                                    
                          
  

const PHASE_PRESENTATION         
                    
            
                      
                            
                              
    
  = {
  unavailable: {
    stateLabel: 'Unavailable',
    stateDescription: 'Voice is unavailable for the current session.',
    motion: 'quiet'
  },
  idle: {
    stateLabel: 'Ready',
    stateDescription: 'App-wide voice is ready. Listening has not started.',
    motion: 'quiet'
  },
  listening: {
    stateLabel: 'Listening',
    stateDescription: 'Listening in the current context.',
    motion: 'receptive'
  },
  transcribing: {
    stateLabel: 'Transcribing',
    stateDescription: 'Turning your speech into text.',
    motion: 'processing'
  },
  thinking: {
    stateLabel: 'Thinking',
    stateDescription: 'Preparing a response for the selected context.',
    motion: 'processing'
  },
  acting: {
    stateLabel: 'Working',
    stateDescription: 'Running an approved action in the selected context.',
    motion: 'active'
  },
  speaking: {
    stateLabel: 'Speaking',
    stateDescription: 'Replying in the selected context.',
    motion: 'expressive'
  },
  attention: {
    stateLabel: 'Needs attention',
    stateDescription: 'Waiting for your decision.',
    motion: 'attention'
  }
}

const STATEMENT_LABELS                                            = {
  fact: 'Fact',
  inference: 'Inference',
  recommendation: 'Recommendation',
  completed: 'Completed'
}

export function vegaPresenceStatementLabel(
  kind                           
)         {
  return STATEMENT_LABELS[kind]
}

export function describeVegaPresence(input            
                          
                         
                             
  )                           {
  const unavailable = input.phase === 'unavailable' || !input.voiceAvailable
  const phase = unavailable ? 'unavailable' : input.phase
  const base = PHASE_PRESENTATION[phase]

  if (input.voiceSessionActive) {
    if (unavailable || phase === 'idle' || phase === 'listening') {
      return {
        phase,
        ...base,
        primaryAction: 'end',
        primaryLabel: 'End voice',
        primaryIcon: 'stop',
        primaryDisabled: false
      }
    }

    return {
      phase,
      ...base,
      primaryAction: 'interrupt',
      primaryLabel: 'Interrupt Vega',
      primaryIcon: 'microphone',
      primaryDisabled: false
    }
  }

  if (unavailable) {
    return {
      phase,
      ...base,
      primaryAction: 'unavailable',
      primaryLabel: 'Voice unavailable',
      primaryIcon: 'microphone',
      primaryDisabled: true
    }
  }

  return {
    phase,
    ...base,
    primaryAction: 'start',
    primaryLabel: 'Start Vega',
    primaryIcon: 'microphone',
    primaryDisabled: false
  }
}
