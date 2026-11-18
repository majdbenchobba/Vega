type PlanningEvent = {
  id: string
  type: string
  source: string
  status?: 'success' | 'error' | 'pending'
  metadata?: Record<string, unknown>
}

const reminderMutationTypes = new Set([
  'domain.calendar.reminder_saved',
  'domain.calendar.reminder_completed',
  'domain.calendar.reminder_reopened',
  'domain.calendar.reminder_deleted',
  'domain.vega.reminder_saved',
  'domain.vega.reminder_completed',
  'domain.vega.reminder_reopened',
  'domain.vega.reminder_deleted'
])

const calendarMutationTypes = new Set([
  'domain.calendar.event_saved',
  'domain.calendar.event_deleted',
  'domain.vega.calendar_event_saved',
  'domain.vega.calendar_event_deleted'
])

/** Events invalidate a scoped read; they never supply resource content. */
function createMutationTracker(
  mutationTypes: ReadonlySet<string>,
  scopeField: 'reminderScope' | 'calendarScope'
): (event: PlanningEvent, projectId: string | null) => boolean {
  const seen = new Set<string>()
  return (event, projectId) => {
    if (event.status !== 'success' || !mutationTypes.has(event.type) ||
      (event.source !== 'main' && event.source !== 'tool') ||
      !event.id || event.id.length > 256 || seen.has(event.id)) return false
    if (event.metadata?.[scopeField] === 'project' &&
      (projectId === null || event.metadata.projectId !== projectId)) return false
    // Personal items can be changed by a run in any project. Delete events may
    // contain only the run's project, so missing resource scope needs a reread.
    seen.add(event.id)
    if (seen.size > 128) seen.delete(seen.values().next().value!)
    return true
  }
}

export function createReminderMutationTracker() {
  return createMutationTracker(reminderMutationTypes, 'reminderScope')
}

export function createCalendarMutationTracker() {
  return createMutationTracker(calendarMutationTypes, 'calendarScope')
}

/** Reviews span the owner's scopes. Only Core commit events trigger a fresh read. */
export function createBriefingReviewMutationTracker() {
  const seen = new Set<string>()
  return (event: PlanningEvent): boolean => {
    if (event.source !== 'main' || event.status !== 'success' ||
      (event.type !== 'domain.vega.briefing_acknowledged' &&
        event.type !== 'domain.vega.briefing_acknowledgements_reset') ||
      !event.id || event.id.length > 256 || seen.has(event.id)) return false
    seen.add(event.id)
    if (seen.size > 128) seen.delete(seen.values().next().value!)
    return true
  }
}
