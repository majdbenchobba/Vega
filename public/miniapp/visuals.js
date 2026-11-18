export function icon(doc, name) {
  const node = doc.createElement('span');
  node.className = 'ui-icon icon-' + name;
  node.setAttribute('aria-hidden', 'true');
  return node;
}

export function projectMark(doc, project) {
  const mark = doc.createElement('span');
  const symbol = project ? 'folder' : 'chat';
  mark.className = 'project-mark';
  mark.dataset.brand = project ? 'project' : 'independent';
  mark.setAttribute('aria-hidden', 'true');
  mark.append(icon(doc, symbol));
  return mark;
}
