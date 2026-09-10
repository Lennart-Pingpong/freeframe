/**
 * Whether a drag is carrying files from outside the browser.
 *
 * The grid and the folder targets are already drag surfaces: dragging an asset
 * card sets `application/json` with `effectAllowed = 'move'` so assets can be
 * moved into folders. A handler that does not tell the two apart starts an
 * upload when someone drags a card across the grid, and lights a folder up when
 * someone drags a file over it.
 *
 * `types` is the part of the DataTransfer that is readable during `dragover` --
 * the items themselves are protected until drop -- and a drag coming from the
 * operating system always lists `Files`.
 */
export function carriesFiles(e: React.DragEvent): boolean {
  return Array.from(e.dataTransfer.types).includes("Files");
}

/**
 * Whether the pointer is really over the element handling this event, rather
 * than the event having merely bubbled here.
 *
 * A Radix dialog renders its overlay into `document.body` but stays where it
 * was written in the React tree, and React propagates events along that tree.
 * So a drag over the folder rename or delete confirmation -- both rendered from
 * `FolderCard`, inside the asset grid -- arrives at the grid's own handlers as
 * if it had happened on the grid. The overlay is `fixed inset-0`, so a rect
 * test agrees: the dialog is centred inside the region it covers.
 *
 * The DOM answers what neither can. An overlay portalled to `document.body` is
 * not a descendant of the region, so `contains` is false for exactly the events
 * that only look local, whichever dialog they come from and wherever it sits.
 */
export function pointerIsOver(e: React.DragEvent): boolean {
  return e.currentTarget.contains(e.target as Node);
}

/**
 * Refuse a file drag that nothing wanted.
 *
 * Every element that accepts a file stops the event, so anything still
 * travelling was wanted by nobody -- and left alone it goes to the browser,
 * which navigates the tab to `file:///...` and takes the session with it. Two
 * pixels of gap between two sidebar rows are enough to lose the page that way,
 * which is how this was found.
 *
 * Belongs on the app shell rather than on a page: the header, the sidebar rail
 * and the attribution badge are siblings of the page, not inside it, and the
 * badge floats over the very asset area that advertises itself as a drop
 * target. Attached there it also catches portalled dialogs, which are children
 * of `document.body` in the DOM but of the shell in the React tree.
 */
export function refuseFileDrag(e: React.DragEvent): void {
  if (!carriesFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "none";
}
