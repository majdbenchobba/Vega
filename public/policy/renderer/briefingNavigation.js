                                      
                    
                          
 

                                      
                 
                           
                          
 

                                                              
                   
                                 
 

export function validCalendarBriefingTarget(target                        )          {
  return validId(target.eventId) && Number.isSafeInteger(target.occurrenceStartAt) &&
    target.occurrenceStartAt >= 0 && target.occurrenceStartAt <= 4_102_444_800_000 &&
    (target.projectId === null || validId(target.projectId))
}

function validId(value         )                  {
  return typeof value === 'string' && value.length > 0 && value.length <= 256 &&
    value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)
}

function matchesScope(
  record                                                          ,
  targetProjectId               ,
  contextProjectId               
)          {
  return targetProjectId === null
    ? record.scope === 'user' && record.project_id === null
    : validId(targetProjectId) && contextProjectId === targetProjectId &&
      record.scope === 'project' && record.project_id === targetProjectId
}

/** Select fresh local data by identity; a briefing label is never source data. */
export function findReminderBriefingTarget            
                                                                  
  (
  records              ,
  target                        ,
  contextProjectId               
)                {
  if (!validId(target.reminderId)) return undefined
  return records.find((record) =>
    record.id === target.reminderId && matchesScope(record, target.projectId, contextProjectId))
}

export function findCalendarBriefingTarget            
                                                                                    
  (
  records              ,
  target                        ,
  contextProjectId               
)                {
  if (!validCalendarBriefingTarget(target)) return undefined
  return records.find((record) =>
    record.id === target.eventId && record.start_at === target.occurrenceStartAt &&
    matchesScope(record, target.projectId, contextProjectId))
}
