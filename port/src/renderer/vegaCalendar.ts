import type { CalendarEvent } from '../preload'

export const CALENDAR_GRID_DAY_COUNT = 42
export const CALENDAR_AGENDA_DAY_COUNT = 30
export type CalendarView = 'month' | 'week' | 'day' | 'agenda'

function localDay(timestamp: number): Date {
  const value = new Date(timestamp)
  value.setHours(0, 0, 0, 0)
  return value
}

function localDayOrdinal(timestamp: number): number {
  const value = new Date(timestamp)
  return Math.trunc(Date.UTC(
    value.getFullYear(),
    value.getMonth(),
    value.getDate()
  ) / (24 * 60 * 60_000))
}

function shiftLocalCalendarDays(timestamp: number, dayDelta: number): number {
  const value = new Date(timestamp)
  value.setDate(value.getDate() + dayDelta)
  return value.getTime()
}

export function localDayStart(timestamp: number): number {
  return localDay(timestamp).getTime()
}

export function monthAnchor(timestamp: number): number {
  const value = new Date(timestamp)
  value.setDate(1)
  value.setHours(0, 0, 0, 0)
  return value.getTime()
}

export function calendarGridDays(anchorTimestamp: number): number[] {
  const first = new Date(monthAnchor(anchorTimestamp))
  const mondayOffset = (first.getDay() + 6) % 7
  first.setDate(first.getDate() - mondayOffset)
  return Array.from({ length: CALENDAR_GRID_DAY_COUNT }, (_, index) => {
    const day = new Date(first)
    day.setDate(first.getDate() + index)
    return day.getTime()
  })
}

export function calendarGridRange(anchorTimestamp: number): { start: number; end: number } {
  const days = calendarGridDays(anchorTimestamp)
  const end = new Date(days[days.length - 1]!)
  end.setDate(end.getDate() + 1)
  return { start: days[0]!, end: end.getTime() }
}

export function calendarWeekDays(anchorTimestamp: number): number[] {
  const first = localDay(anchorTimestamp)
  const mondayOffset = (first.getDay() + 6) % 7
  first.setDate(first.getDate() - mondayOffset)
  return Array.from({ length: 7 }, (_, index) => {
    const day = new Date(first)
    day.setDate(first.getDate() + index)
    return day.getTime()
  })
}

export function calendarWeekRange(anchorTimestamp: number): { start: number; end: number } {
  const days = calendarWeekDays(anchorTimestamp)
  return { start: days[0]!, end: nextLocalDay(days[days.length - 1]!) }
}

export function calendarDayRange(anchorTimestamp: number): { start: number; end: number } {
  const start = localDayStart(anchorTimestamp)
  return { start, end: nextLocalDay(start) }
}

export function calendarAgendaDays(
  anchorTimestamp: number,
  dayCount = CALENDAR_AGENDA_DAY_COUNT
): number[] {
  const boundedDayCount = Math.max(1, Math.min(366, Math.trunc(dayCount)))
  const first = localDay(anchorTimestamp)
  return Array.from({ length: boundedDayCount }, (_, index) => {
    const day = new Date(first)
    day.setDate(first.getDate() + index)
    return day.getTime()
  })
}

export function calendarAgendaRange(
  anchorTimestamp: number,
  dayCount = CALENDAR_AGENDA_DAY_COUNT
): { start: number; end: number } {
  const days = calendarAgendaDays(anchorTimestamp, dayCount)
  return { start: days[0]!, end: nextLocalDay(days[days.length - 1]!) }
}

export function calendarViewRange(
  view: CalendarView,
  anchorTimestamp: number
): { start: number; end: number } {
  if (view === 'month') return calendarGridRange(anchorTimestamp)
  if (view === 'week') return calendarWeekRange(anchorTimestamp)
  if (view === 'agenda') return calendarAgendaRange(anchorTimestamp)
  return calendarDayRange(anchorTimestamp)
}

export function moveCalendarAnchor(
  anchorTimestamp: number,
  view: CalendarView,
  direction: -1 | 1
): number {
  const value = localDay(anchorTimestamp)
  if (view === 'month') {
    value.setDate(1)
    value.setMonth(value.getMonth() + direction)
  } else {
    const days = view === 'week' ? 7 : view === 'agenda' ? CALENDAR_AGENDA_DAY_COUNT : 1
    value.setDate(value.getDate() + direction * days)
  }
  return value.getTime()
}

export function nextLocalDay(timestamp: number): number {
  const value = localDay(timestamp)
  value.setDate(value.getDate() + 1)
  return value.getTime()
}

export function calendarEventsForDay(
  events: readonly CalendarEvent[],
  dayTimestamp: number
): CalendarEvent[] {
  const start = localDay(dayTimestamp).getTime()
  const end = nextLocalDay(start)
  return events
    .filter((event) => event.end_at > start && event.start_at < end)
    .sort((left, right) =>
      Number(right.all_day) - Number(left.all_day) ||
      left.start_at - right.start_at ||
      left.title.localeCompare(right.title)
    )
}

export function moveCalendarSeriesToDay(
  event: CalendarEvent,
  sourceDayTimestamp: number,
  targetDayTimestamp: number
): {
  startAt: number
  endAt: number
  recurrenceUntil: number | null
} {
  const dayDelta =
    localDayOrdinal(targetDayTimestamp) -
    localDayOrdinal(sourceDayTimestamp)
  const startAt = shiftLocalCalendarDays(event.series_start_at, dayDelta)
  const endAt = event.all_day
    ? shiftLocalCalendarDays(event.series_end_at, dayDelta)
    : startAt + (event.series_end_at - event.series_start_at)
  return {
    startAt,
    endAt,
    recurrenceUntil: event.recurrence_until === null
      ? null
      : shiftLocalCalendarDays(event.recurrence_until, dayDelta)
  }
}

export function localDateValue(timestamp: number): string {
  const value = new Date(timestamp)
  const year = String(value.getFullYear()).padStart(4, '0')
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function localDateTimeValue(timestamp: number): string {
  const value = new Date(timestamp)
  const hours = String(value.getHours()).padStart(2, '0')
  const minutes = String(value.getMinutes()).padStart(2, '0')
  return `${localDateValue(timestamp)}T${hours}:${minutes}`
}

export function parseLocalDateValue(value: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const [year, month, day] = value.split('-').map(Number)
  const parsed = new Date(year!, month! - 1, day!)
  parsed.setHours(0, 0, 0, 0)
  if (
    parsed.getFullYear() !== year ||
    parsed.getMonth() !== month! - 1 ||
    parsed.getDate() !== day
  ) return null
  return parsed.getTime()
}

export function inclusiveDateEndTimestamp(value: string): number | null {
  const parsed = parseLocalDateValue(value)
  return parsed === null ? null : nextLocalDay(parsed)
}

export function sameLocalDay(left: number, right: number): boolean {
  return localDateValue(left) === localDateValue(right)
}
