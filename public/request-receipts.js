// Shared receipt wording. A routing receipt never establishes completed work.
export const requestLabel = state => ({
  awaiting_input: 'Needs details', queued: 'Queued', routing: 'Routing', handled: 'Routed',
  cancelled: 'Cancelled', partially_cancelled: 'Partly cancelled', stale_target: 'Connection changed',
  needs_attention: 'Needs attention', failed: 'Failed', uncertain: 'Unconfirmed', expired: 'Expired',
  delivery_pending: 'Delivery pending', delivery_failed: 'Delivery failed', delivered: 'Delivered',
})[state] || 'Status unavailable';

export const requestSummary = state => ({
  awaiting_input: 'Add the missing details.',
  queued: 'Waiting for routing.',
  routing: 'Routing started. Receipt pending.',
  handled: 'Routing recorded. Completion not confirmed.',
  cancelled: 'Cancelled before routing.',
  partially_cancelled: 'Some routes were cancelled. Check details.',
  stale_target: 'Review the project connection before sending again.',
  needs_attention: 'Check the route details before trying again.',
  failed: 'The request failed. Check details.',
  uncertain: 'No confirmed receipt. Refresh before retrying.',
  expired: 'The request expired.',
})[state] || 'Check the recorded details.';

export const receiptSummary = (text, state) => state === 'handled' && /unverified|not verified/i.test(text || '')
  ? 'Linked to existing work. Completion is still unverified.' : requestSummary(state);

export const referenceTokens = text => [...new Set(String(text || '').match(
  /\b(?:prior[a-f0-9-]{8,36}|turn[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.(?:md|txt))\b/g
) || [])];
