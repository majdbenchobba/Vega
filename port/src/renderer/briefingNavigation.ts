export type ReminderBriefingTarget = {
  reminderId: string
  projectId: string | null
}

export type CalendarBriefingTarget = {
  eventId: string
  occurrenceStartAt: number
  projectId: string | null
}

export type CalendarBriefingFocus = CalendarBriefingTarget & {
  requestId: number
  contextProjectId: string | null
}

export function validCalendarBriefingTarget(target: CalendarBriefingTarget): boolean {
  return validId(target.eventId) && Number.isSafeInteger(target.occurrenceStartAt) &&
    target.occurrenceStartAt >= 0 && target.occurrenceStartAt <= 4_102_444_800_000 &&
    (target.projectId === null || validId(target.projectId))
}

function validId(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256 &&
    value.trim() === value && !/[\x00-\x1f\x7f]/.test(value)
}

function matchesScope(
  record: { scope: 'user' | 'project'; project_id: string | null },
  targetProjectId: string | null,
  contextProjectId: string | null
): boolean {
  return targetProjectId === null
    ? record.scope === 'user' && record.project_id === null
    : validId(targetProjectId) && contextProjectId === targetProjectId &&
      record.scope === 'project' && record.project_id === targetProjectId
}

/** Select fresh local data by identity; a briefing label is never source data. */
export function findReminderBriefingTarget<T extends {
  id: string; scope: 'user' | 'project'; project_id: string | null
}>(
  records: readonly T[],
  target: ReminderBriefingTarget,
  contextProjectId: string | null
): T | undefined {
  if (!validId(target.reminderId)) return undefined
  return records.find((record) =>
    record.id === target.reminderId && matchesScope(record, target.projectId, contextProjectId))
}

export function findCalendarBriefingTarget<T extends {
  id: string; start_at: number; scope: 'user' | 'project'; project_id: string | null
}>(
  records: readonly T[],
  target: CalendarBriefingTarget,
  contextProjectId: string | null
): T | undefined {
  if (!validCalendarBriefingTarget(target)) return undefined
  return records.find((record) =>
    record.id === target.eventId && record.start_at === target.occurrenceStartAt &&
    matchesScope(record, target.projectId, contextProjectId))
}
