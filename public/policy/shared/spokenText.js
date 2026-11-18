/** Presentational cleanup only. Never rewrite or "correct" the user's transcript. */
export function spokenResponseText(text        , limit = 4096)         {
  const plain = text.slice(0, 100_000)
    .replace(/^```[^\n]*$/gm, '')
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+)/gm, '')
    .replace(/\s+/g, ' ').trim()
  if (plain.length <= limit) return plain
  const suffix = ' The rest is in the chat.'
  const available = Math.max(0, limit - suffix.length)
  const prefix = plain.slice(0, available)
  const boundary = Math.max(prefix.lastIndexOf('. '), prefix.lastIndexOf('? '), prefix.lastIndexOf('! '))
  return (boundary > available * .6 ? prefix.slice(0, boundary + 1) : prefix.slice(0, prefix.lastIndexOf(' '))).trim() + suffix
}
