import type { VegaBriefingAcknowledgement } from '../shared/vegaBriefing.ts'
import type { CalendarBriefingTarget, ReminderBriefingTarget } from './briefingNavigation.ts'

export const VEGA_BRIEFING_SCOPES_STORAGE_KEY = 'vega.vega-briefing-scopes.v1'
export const VEGA_BRIEFING_CONVERSATION_SUMMARY_CARD_BUDGET = 1
export const VEGA_BRIEFING_PROJECT_SUMMARY_CARD_BUDGET = 1
export const VEGA_BRIEFING_SUGGESTION_CARD_BUDGET = 3
export const VEGA_BRIEFING_REMINDER_CARD_BUDGET = 5
export const VEGA_BRIEFING_CALENDAR_CARD_BUDGET = 5
export const VEGA_BRIEFING_CALENDAR_WINDOW_MS = 24 * 60 * 60_000

export type VegaBriefingScopes = {
  summaries: boolean
  suggestions: boolean
  conversation: boolean
  project: boolean
  release: boolean
  reminders: boolean
  calendar: boolean
}

export const DEFAULT_VEGA_BRIEFING_SCOPES: VegaBriefingScopes = {
  summaries: true,
  suggestions: true,
  conversation: true,
  project: true,
  release: true,
  reminders: true,
  calendar: true
}

export const DISABLED_VEGA_BRIEFING_SCOPES: VegaBriefingScopes = {
  summaries: false,
  suggestions: false,
  conversation: false,
  project: false,
  release: false,
  reminders: false,
  calendar: false
}

export type VegaBriefingTask = {
  id: string
  conversationId: string
  projectId: string | null
  status: 'active' | 'paused'
  summary: string
}

export type VegaBriefingConversationSummary = {
  conversationId: string
  userMessageCount: number
  assistantMessageCount: number
  lastUserMessageId: string | null
  lastAssistantMessageId: string | null
  lastUserText: string | null
  lastAssistantText: string | null
  updatedAt: number
}

export type VegaBriefingProjectConversation = {
  id: string
  title: string
  updatedAt: number
}

export type VegaBriefingFailure = {
  id: string
  conversationId: string | null
  projectId: string | null
  title: string
  detail: string
}

export type VegaBriefingReminder = {
  id: string
  scope: 'user' | 'project'
  projectId: string | null
  title: string
  note: string
  sourceUrl: string | null
  dueAt: number
  completedAt: number | null
  revision: number
}

export type VegaBriefingCalendarEvent = {
  id: string
  scope: 'user' | 'project'
  projectId: string | null
  kind: 'event' | 'time-block'
  title: string
  note: string
  startAt: number
  endAt: number
  allDay: boolean
  timeZone: string
  recurrence: 'none' | 'daily' | 'weekly' | 'monthly'
  recurrenceUntil: number | null
  updatedAt: number
}

export type VegaBriefingAction =
  | { kind: 'review-approvals'; conversationId: string }
  | { kind: 'open-conversation'; conversationId: string }
  | { kind: 'open-tasks'; conversationId: string | null }
  | { kind: 'open-activity' }
  | { kind: 'open-settings' }
  | { kind: 'open-reminders' }
  | { kind: 'open-calendar' }
  | ({ kind: 'open-reminder' } & ReminderBriefingTarget)
  | ({ kind: 'open-calendar-item' } & CalendarBriefingTarget)

export type VegaBriefingItem = {
  id: string
  signalKey: string
  revision: string
  kind: 'summary' | 'reminder' | 'suggestion'
  title: string
  detail: string
  actionLabel: string
  action: VegaBriefingAction
}

export type VegaBriefing = {
  headline: string
  detail: string
  items: VegaBriefingItem[]
}

export type VegaBriefingInput = {
  scopes: VegaBriefingScopes
  conversationId: string | null
  projectId: string | null
  conversationSummary?: VegaBriefingConversationSummary | null
  projectName?: string | null
  projectConversations?: readonly VegaBriefingProjectConversation[]
  pendingApprovalIds: readonly string[]
  queuedMessageIds: readonly string[]
  tasks: readonly VegaBriefingTask[]
  reminders: readonly VegaBriefingReminder[]
  calendarEvents: readonly VegaBriefingCalendarEvent[]
  recentFailure: VegaBriefingFailure | null
  updatePhase: 'disabled' | 'idle' | 'checking' | 'available' | 'downloaded' | 'not-available' | 'error' | null
  now: number
}

function parsedScopes(value: unknown): VegaBriefingScopes | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const candidate = value as Partial<Record<keyof VegaBriefingScopes, unknown>>
  if (
    typeof candidate.conversation !== 'boolean' ||
    typeof candidate.project !== 'boolean' ||
    typeof candidate.release !== 'boolean' ||
    (candidate.summaries !== undefined && typeof candidate.summaries !== 'boolean') ||
    (candidate.suggestions !== undefined && typeof candidate.suggestions !== 'boolean') ||
    (candidate.reminders !== undefined && typeof candidate.reminders !== 'boolean') ||
    (candidate.calendar !== undefined && typeof candidate.calendar !== 'boolean')
  ) return null
  return {
    summaries: candidate.summaries ?? true,
    suggestions: candidate.suggestions ?? true,
    conversation: candidate.conversation,
    project: candidate.project,
    release: candidate.release,
    reminders: candidate.reminders ?? true,
    calendar: candidate.calendar ?? true
  }
}

export function restoreVegaBriefingScopes(raw: string | null): VegaBriefingScopes {
  if (raw === null) return { ...DEFAULT_VEGA_BRIEFING_SCOPES }
  try {
    return parsedScopes(JSON.parse(raw)) ?? { ...DISABLED_VEGA_BRIEFING_SCOPES }
  } catch {
    return { ...DISABLED_VEGA_BRIEFING_SCOPES }
  }
}

export function serializeVegaBriefingScopes(scopes: VegaBriefingScopes): string {
  return JSON.stringify(parsedScopes(scopes) ?? DISABLED_VEGA_BRIEFING_SCOPES)
}

function cleanSummary(value: string): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  if (!normalized) return 'Untitled task'
  return normalized.length <= 120 ? normalized : `${normalized.slice(0, 117)}…`
}

function cleanDetail(value: string): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length <= 160 ? normalized : `${normalized.slice(0, 157)}…`
}

function cleanConversationExcerpt(value: string | null): string {
  if (!value) return ''
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length <= 64 ? normalized : `${normalized.slice(0, 61)}…`
}

function conversationSummaryDetail(summary: VegaBriefingConversationSummary): string {
  const counts = [
    summary.userMessageCount
      ? `${summary.userMessageCount} request${summary.userMessageCount === 1 ? '' : 's'}`
      : '',
    summary.assistantMessageCount
      ? `${summary.assistantMessageCount} repl${summary.assistantMessageCount === 1 ? 'y' : 'ies'}`
      : ''
  ].filter(Boolean).join(' · ')
  const request = cleanConversationExcerpt(summary.lastUserText)
  const reply = cleanConversationExcerpt(summary.lastAssistantText)
  return cleanDetail([
    counts ? `${counts}.` : '',
    request ? `Latest request: ${request}.` : '',
    reply ? `Latest reply: ${reply}.` : 'No assistant reply is saved yet.'
  ].filter(Boolean).join(' '))
}

function projectSummaryDetail(input: {
  conversationCount: number
  latestConversation: VegaBriefingProjectConversation
  activeTaskCount: number
  pausedTaskCount: number
}): string {
  const conversations = `${input.conversationCount} conversation${input.conversationCount === 1 ? '' : 's'}.`
  const latest = `Latest: ${cleanConversationExcerpt(input.latestConversation.title)}.`
  const tasks = taskCountsLabel(input.activeTaskCount, input.pausedTaskCount)
  return cleanDetail([
    conversations,
    latest,
    tasks ? `${tasks} project task${input.activeTaskCount + input.pausedTaskCount === 1 ? '' : 's'}.` : ''
  ].filter(Boolean).join(' '))
}

function dueDetail(dueAt: number, currentTime: number): string {
  const overdueMs = Math.max(0, currentTime - dueAt)
  if (overdueMs < 60_000) return 'Due now.'
  const minutes = Math.floor(overdueMs / 60_000)
  if (minutes < 60) return `Overdue by ${minutes} minute${minutes === 1 ? '' : 's'}.`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `Overdue by ${hours} hour${hours === 1 ? '' : 's'}.`
  const days = Math.floor(hours / 24)
  return `Overdue by ${days} day${days === 1 ? '' : 's'}.`
}

function calendarDateTime(timestamp: number, timeZone: string, allDay: boolean): string {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone,
      month: 'short',
      day: 'numeric',
      ...(allDay ? {} : { hour: 'numeric', minute: '2-digit' })
    }).format(timestamp)
  } catch {
    return new Date(timestamp).toISOString()
  }
}

function calendarDetail(event: VegaBriefingCalendarEvent, currentTime: number): string {
  const note = cleanDetail(event.note)
  const scope = event.scope === 'project' ? 'Current project.' : 'Personal.'
  if (event.allDay) {
    const start = calendarDateTime(event.startAt, event.timeZone, true)
    const inclusiveEnd = calendarDateTime(Math.max(event.startAt, event.endAt - 1), event.timeZone, true)
    const timing = start === inclusiveEnd ? `All day ${start}.` : `All day ${start}–${inclusiveEnd}.`
    return `${timing} ${scope}${note ? ` ${note}` : ''}`
  }
  const status = event.startAt <= currentTime ? 'In progress' : 'Scheduled'
  const start = calendarDateTime(event.startAt, event.timeZone, false)
  const end = calendarDateTime(event.endAt, event.timeZone, false)
  return `${status} · ${start}–${end} (${event.timeZone}). ${scope}${note ? ` ${note}` : ''}`
}

function taskCountsLabel(active: number, paused: number): string {
  const parts = [
    active ? `${active} active` : '',
    paused ? `${paused} paused` : ''
  ].filter(Boolean)
  return parts.join(' · ')
}

function signalRevision(parts: readonly string[]): string {
  const source = parts.join('\u001f')
  let hash = 14_695_981_039_346_656_037n
  for (let index = 0; index < source.length; index += 1) {
    hash ^= BigInt(source.charCodeAt(index))
    hash = BigInt.asUintN(64, hash * 1_099_511_628_211n)
  }
  return `${parts.length}:${source.length}:${hash.toString(16).padStart(16, '0')}`
}

function item(input: Omit<VegaBriefingItem, 'id' | 'revision'> & { revisionParts: readonly string[] }): VegaBriefingItem {
  const revision = signalRevision(input.revisionParts)
  return {
    id: `${input.signalKey}@${revision}`,
    signalKey: input.signalKey,
    revision,
    kind: input.kind,
    title: input.title,
    detail: input.detail,
    actionLabel: input.actionLabel,
    action: input.action
  }
}

function summarizeItems(items: VegaBriefingItem[], emptyDetail: string): VegaBriefing {
  if (items.length === 0) {
    return {
      headline: 'You are caught up',
      detail: emptyDetail,
      items: []
    }
  }
  const reminders = items.filter((briefingItem) => briefingItem.kind === 'reminder').length
  const suggestions = items.filter((briefingItem) => briefingItem.kind === 'suggestion').length
  const summaries = items.length - reminders - suggestions
  const parts = [
    reminders ? `${reminders} reminder${reminders === 1 ? '' : 's'}` : '',
    suggestions ? `${suggestions} suggestion${suggestions === 1 ? '' : 's'}` : '',
    summaries ? `${summaries} status update${summaries === 1 ? '' : 's'}` : ''
  ].filter(Boolean)

  return {
    headline: `${items.length} item${items.length === 1 ? '' : 's'} for your attention`,
    detail: parts.join(' · '),
    items
  }
}

export function applyVegaBriefingAcknowledgements(
  briefing: VegaBriefing,
  acknowledgements: readonly VegaBriefingAcknowledgement[]
): { briefing: VegaBriefing; acknowledgedCount: number } {
  if (briefing.items.length === 0) return { briefing, acknowledgedCount: 0 }
  const acknowledged = new Map(acknowledgements.map((entry) => [entry.signalKey, entry.revision]))
  const visible = briefing.items.filter((briefingItem) =>
    acknowledged.get(briefingItem.signalKey) !== briefingItem.revision)
  const acknowledgedCount = briefing.items.length - visible.length
  return {
    briefing: summarizeItems(
      visible,
      acknowledgedCount
        ? `${acknowledgedCount} reviewed item${acknowledgedCount === 1 ? '' : 's'} will stay quiet until the underlying local signal changes.`
        : briefing.detail
    ),
    acknowledgedCount
  }
}

export function buildVegaBriefing(input: VegaBriefingInput): VegaBriefing {
  if (
    !input.scopes.summaries &&
    !input.scopes.suggestions &&
    !input.scopes.conversation &&
    !input.scopes.project &&
    !input.scopes.release &&
    !input.scopes.reminders &&
    !input.scopes.calendar
  ) {
    return {
      headline: 'Briefing is quiet',
      detail: 'All proactive Vega briefing scopes are off.',
      items: []
    }
  }
  const items: VegaBriefingItem[] = []
  const conversationSummaries =
    input.scopes.summaries &&
    input.conversationId &&
    input.conversationSummary?.conversationId === input.conversationId &&
    (input.conversationSummary.lastUserText || input.conversationSummary.lastAssistantText)
      ? [input.conversationSummary].slice(0, VEGA_BRIEFING_CONVERSATION_SUMMARY_CARD_BUDGET)
      : []
  for (const summary of conversationSummaries) {
    items.push(item({
      signalKey: `conversation-summary:${summary.conversationId}`,
      revisionParts: [
        summary.lastUserMessageId ?? '',
        summary.lastAssistantMessageId ?? '',
        String(summary.userMessageCount),
        String(summary.assistantMessageCount),
        String(summary.updatedAt),
        cleanDetail(summary.lastUserText ?? ''),
        cleanDetail(summary.lastAssistantText ?? '')
      ],
      kind: 'summary',
      title: 'Conversation recap',
      detail: conversationSummaryDetail(summary),
      actionLabel: 'Open conversation',
      action: { kind: 'open-conversation', conversationId: summary.conversationId }
    }))
  }
  const projectConversations =
    input.scopes.summaries &&
    input.scopes.project &&
    input.projectId
      ? [...(input.projectConversations ?? [])]
        .filter((conversation) =>
          Boolean(conversation.id) &&
          Boolean(conversation.title.trim()) &&
          Number.isSafeInteger(conversation.updatedAt))
        .sort((left, right) =>
          right.updatedAt - left.updatedAt ||
          left.id.localeCompare(right.id))
      : []
  const projectSummaryConversations = projectConversations
    .slice(0, VEGA_BRIEFING_PROJECT_SUMMARY_CARD_BUDGET)
  const conversationTasks = input.scopes.conversation && input.conversationId
    ? input.tasks.filter((task) => task.conversationId === input.conversationId)
    : []
  const projectTasks = input.scopes.project && input.projectId
    ? input.tasks.filter((task) =>
      task.projectId === input.projectId &&
      (!input.conversationId || task.conversationId !== input.conversationId))
    : []
  for (const latestConversation of projectSummaryConversations) {
    const projectTaskCount = input.tasks.filter((task) => task.projectId === input.projectId)
    const activeTaskCount = projectTaskCount.filter((task) => task.status === 'active').length
    const pausedTaskCount = projectTaskCount.length - activeTaskCount
    items.push(item({
      signalKey: `project-summary:${input.projectId}`,
      revisionParts: [
        input.projectName ?? '',
        String(projectConversations.length),
        latestConversation.id,
        latestConversation.title,
        String(latestConversation.updatedAt),
        String(activeTaskCount),
        String(pausedTaskCount)
      ],
      kind: 'summary',
      title: input.projectName?.trim()
        ? `Project recap · ${cleanSummary(input.projectName)}`
        : 'Project recap',
      detail: projectSummaryDetail({
        conversationCount: projectConversations.length,
        latestConversation,
        activeTaskCount,
        pausedTaskCount
      }),
      actionLabel: 'Open latest conversation',
      action: { kind: 'open-conversation', conversationId: latestConversation.id }
    }))
  }

  const dueReminders = input.scopes.reminders
    ? input.reminders
      .filter((reminder) =>
        reminder.completedAt === null &&
        reminder.dueAt <= input.now &&
        (
          reminder.scope === 'user' ||
          (reminder.scope === 'project' && reminder.projectId === input.projectId)
        ))
      .sort((left, right) => left.dueAt - right.dueAt || left.id.localeCompare(right.id))
    : []
  for (const reminder of dueReminders.slice(0, VEGA_BRIEFING_REMINDER_CARD_BUDGET)) {
    const note = cleanDetail(reminder.note)
    items.push(item({
      signalKey: `reminder:${reminder.id}`,
      revisionParts: [
        reminder.title,
        reminder.note,
        reminder.sourceUrl ?? '',
        String(reminder.dueAt),
        String(reminder.revision)
      ],
      kind: 'reminder',
      title: cleanSummary(reminder.title),
      detail: `${dueDetail(reminder.dueAt, input.now)}${note ? ` ${note}` : ''}`,
      actionLabel: 'Show reminder',
      action: { kind: 'open-reminder', reminderId: reminder.id, projectId: reminder.projectId }
    }))
  }
  if (input.scopes.suggestions && dueReminders.length > VEGA_BRIEFING_REMINDER_CARD_BUDGET) {
    const remaining = dueReminders.slice(VEGA_BRIEFING_REMINDER_CARD_BUDGET)
    items.push(item({
      signalKey: `reminder:overflow:${input.projectId ?? 'user'}`,
      revisionParts: remaining.map((reminder) => `${reminder.id}:${reminder.dueAt}:${reminder.revision}`).sort(),
      kind: 'suggestion',
      title: `${remaining.length} more due reminder${remaining.length === 1 ? '' : 's'}`,
      detail: `Vega shows at most ${VEGA_BRIEFING_REMINDER_CARD_BUDGET} reminder cards in one briefing.`,
      actionLabel: 'Review reminders',
      action: { kind: 'open-reminders' }
    }))
  }

  const calendarWindowEnd = input.now + VEGA_BRIEFING_CALENDAR_WINDOW_MS
  const upcomingCalendarEvents = input.scopes.calendar
    ? input.calendarEvents
      .filter((event) =>
        Number.isSafeInteger(event.startAt) &&
        Number.isSafeInteger(event.endAt) &&
        event.endAt > input.now &&
        event.startAt < calendarWindowEnd &&
        (
          event.scope === 'user' ||
          (event.scope === 'project' && event.projectId === input.projectId)
        ))
      .sort((left, right) =>
        left.startAt - right.startAt ||
        left.endAt - right.endAt ||
        left.id.localeCompare(right.id))
    : []
  for (const event of upcomingCalendarEvents.slice(0, VEGA_BRIEFING_CALENDAR_CARD_BUDGET)) {
    items.push(item({
      signalKey: `calendar:${event.id}:${event.startAt}`,
      revisionParts: [
        event.scope,
        event.projectId ?? '',
        event.kind,
        event.title,
        event.note,
        String(event.startAt),
        String(event.endAt),
        String(event.allDay),
        event.timeZone,
        event.recurrence,
        String(event.recurrenceUntil ?? ''),
        String(event.updatedAt)
      ],
      kind: event.startAt <= input.now ? 'reminder' : 'summary',
      title: cleanSummary(event.title),
      detail: calendarDetail(event, input.now),
      actionLabel: 'Show in Calendar',
      action: { kind: 'open-calendar-item', eventId: event.id, occurrenceStartAt: event.startAt, projectId: event.projectId }
    }))
  }
  if (input.scopes.suggestions && upcomingCalendarEvents.length > VEGA_BRIEFING_CALENDAR_CARD_BUDGET) {
    const remaining = upcomingCalendarEvents.slice(VEGA_BRIEFING_CALENDAR_CARD_BUDGET)
    items.push(item({
      signalKey: `calendar:overflow:${input.projectId ?? 'user'}`,
      revisionParts: remaining
        .map((event) => `${event.id}:${event.startAt}:${event.endAt}:${event.updatedAt}`)
        .sort(),
      kind: 'suggestion',
      title: `${remaining.length} more calendar item${remaining.length === 1 ? '' : 's'} in the next 24 hours`,
      detail: `Vega shows at most ${VEGA_BRIEFING_CALENDAR_CARD_BUDGET} upcoming calendar cards in one briefing.`,
      actionLabel: 'Review calendar',
      action: { kind: 'open-calendar' }
    }))
  }

  if (input.scopes.conversation && input.conversationId && input.pendingApprovalIds.length > 0) {
    const pendingApprovalIds = [...input.pendingApprovalIds].sort()
    items.push(item({
      signalKey: `approvals:${input.conversationId}`,
      revisionParts: pendingApprovalIds,
      kind: 'reminder',
      title: pendingApprovalIds.length === 1 ? 'One approval is waiting' : `${pendingApprovalIds.length} approvals are waiting`,
      detail: 'Review the immutable action before the assistant can continue.',
      actionLabel: 'Review',
      action: { kind: 'review-approvals', conversationId: input.conversationId }
    }))
  }

  if (input.scopes.conversation && input.conversationId && input.queuedMessageIds.length > 0) {
    const queuedMessageIds = [...input.queuedMessageIds].sort()
    items.push(item({
      signalKey: `queue:${input.conversationId}`,
      revisionParts: queuedMessageIds,
      kind: 'summary',
      title: queuedMessageIds.length === 1 ? 'One follow-up is queued' : `${queuedMessageIds.length} follow-ups are queued`,
      detail: 'The current conversation will process them in order unless you stop it.',
      actionLabel: 'Open thread',
      action: { kind: 'open-conversation', conversationId: input.conversationId }
    }))
  }

  if (conversationTasks.length > 0) {
    const active = conversationTasks.filter((task) => task.status === 'active').length
    const paused = conversationTasks.length - active
    const first = conversationTasks.find((task) => task.status === 'paused') ?? conversationTasks[0]
    items.push(item({
      signalKey: `conversation-tasks:${input.conversationId}`,
      revisionParts: conversationTasks
        .map((task) => `${task.id}:${task.status}:${cleanSummary(task.summary)}`)
        .sort(),
      kind: paused ? 'reminder' : 'summary',
      title: paused ? `Resume ${cleanSummary(first.summary)}` : cleanSummary(first.summary),
      detail: `${taskCountsLabel(active, paused)} in this conversation.`,
      actionLabel: 'Open tasks',
      action: { kind: 'open-tasks', conversationId: input.conversationId }
    }))
  }

  if (input.scopes.suggestions && projectTasks.length > 0) {
    const active = projectTasks.filter((task) => task.status === 'active').length
    const paused = projectTasks.length - active
    items.push(item({
      signalKey: `project-tasks:${input.projectId}`,
      revisionParts: projectTasks
        .map((task) => `${task.id}:${task.status}:${cleanSummary(task.summary)}`)
        .sort(),
      kind: 'suggestion',
      title: projectTasks.length === 1 ? 'One other project task needs attention' : `${projectTasks.length} other project tasks need attention`,
      detail: `${taskCountsLabel(active, paused)} outside the current conversation.`,
      actionLabel: 'Review project',
      action: { kind: 'open-tasks', conversationId: projectTasks[0]?.conversationId ?? null }
    }))
  }

  const failureMatchesConversation =
    input.scopes.conversation &&
    input.conversationId &&
    input.recentFailure?.conversationId === input.conversationId
  const failureMatchesProject =
    input.scopes.project &&
    input.projectId &&
    input.recentFailure?.projectId === input.projectId
  if (input.scopes.suggestions && input.recentFailure && (failureMatchesConversation || failureMatchesProject)) {
    items.push(item({
      signalKey: `failure:${input.recentFailure.conversationId ?? input.recentFailure.projectId ?? 'unscoped'}`,
      revisionParts: [
        input.recentFailure.id,
        cleanSummary(input.recentFailure.title),
        cleanSummary(input.recentFailure.detail)
      ],
      kind: 'suggestion',
      title: `Review ${cleanSummary(input.recentFailure.title)}`,
      detail: cleanSummary(input.recentFailure.detail),
      actionLabel: 'Open activity',
      action: { kind: 'open-activity' }
    }))
  }

  if (input.scopes.release && input.updatePhase === 'downloaded') {
    items.push(item({
      signalKey: 'desktop-update:current',
      revisionParts: ['downloaded'],
      kind: 'reminder',
      title: 'A desktop update is ready',
      detail: 'Restart only after active work and unsent drafts are safe.',
      actionLabel: 'Open settings',
      action: { kind: 'open-settings' }
    }))
  } else if (input.scopes.suggestions && input.scopes.release && input.updatePhase === 'error') {
    items.push(item({
      signalKey: 'desktop-update:current',
      revisionParts: ['error'],
      kind: 'suggestion',
      title: 'Desktop update needs review',
      detail: 'Open Settings to inspect the fixed update status and retry deliberately.',
      actionLabel: 'Open settings',
      action: { kind: 'open-settings' }
    }))
  }

  let suggestionCount = 0
  const boundedItems = items.filter((briefingItem) => {
    if (briefingItem.kind !== 'suggestion') return true
    suggestionCount += 1
    return suggestionCount <= VEGA_BRIEFING_SUGGESTION_CARD_BUDGET
  })

  return summarizeItems(
    boundedItems,
    'Vega found no local conversation recap, calendar, reminder, approval, task, failure or release signal needing attention in the selected scopes.'
  )
}
