import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { FolderTree } from '../folder-tree'
import type { FolderTreeNode } from '@/types'

const tree: FolderTreeNode[] = [
  {
    id: 'episodes',
    name: 'Episodes',
    parent_id: null,
    item_count: 2,
    children: [
      { id: 'ep-04', name: 'Episode 04', parent_id: 'episodes', item_count: 1, children: [] },
    ],
  },
]

const noop = async () => {}
const file = new File(['x'], 'cut-v3.mov', { type: 'video/quicktime' })

/** What a browser hands a file drag. `types` is the readable part during
 *  dragover; `files` only fills in on drop. */
function fileDrag(files: File[] = [file]) {
  return { types: ['Files'], files, dropEffect: 'none' }
}

function renderTree(props: Record<string, unknown> = {}) {
  return render(
    <FolderTree
      tree={tree}
      projectName="Season 2"
      currentFolderId={null}
      showTrash={false}
      onSelectFolder={() => {}}
      onShowTrash={() => {}}
      onCreateFolder={noop}
      onRenameFolder={noop}
      onDeleteFolder={noop}
      {...props}
    />,
  )
}

describe('dropping a file on the sidebar tree', () => {
  it('uploads into the row it was dropped on', () => {
    // The regression this pins: FolderTree took the handler and never passed it
    // to the rows, so the feature was dead in the sidebar while the CHANGELOG
    // said otherwise.
    const onDropFiles = vi.fn()
    renderTree({ onDropFiles })

    fireEvent.drop(screen.getByText('Episodes'), { dataTransfer: fileDrag() })

    expect(onDropFiles).toHaveBeenCalledWith('episodes', [file])
  })

  it('uploads into a nested row too', () => {
    const onDropFiles = vi.fn()
    renderTree({ onDropFiles })
    fireEvent.click(screen.getByText('Episodes')) // expands the children

    fireEvent.drop(screen.getByText('Episode 04'), { dataTransfer: fileDrag() })

    expect(onDropFiles).toHaveBeenCalledWith('ep-04', [file])
  })

  it('uploads into the project root when dropped on the project row', () => {
    // The row lit up for a file drag before and then did nothing with it.
    const onDropFiles = vi.fn()
    renderTree({ onDropFiles })

    fireEvent.drop(screen.getByText('Season 2'), { dataTransfer: fileDrag() })

    expect(onDropFiles).toHaveBeenCalledWith(null, [file])
  })

  it('reports an empty file list rather than staying silent', () => {
    // Several promised-file sources on macOS drop with nothing readable. The
    // caller owns the drag marking, so it has to hear about the drop either
    // way or "Drop to upload" stays on screen for good.
    const onDropFiles = vi.fn()
    renderTree({ onDropFiles })

    fireEvent.drop(screen.getByText('Episodes'), { dataTransfer: fileDrag([]) })

    expect(onDropFiles).toHaveBeenCalledWith('episodes', [])
  })

  it('cancels the browser default even where uploads are refused', () => {
    // Without onDropFiles -- a reviewer, or any caller with no upload path --
    // an unprevented file drop navigates the tab to `file:///...`. fireEvent
    // returns false when the default was prevented.
    renderTree()

    const row = screen.getByText('Episodes')
    expect(fireEvent.dragOver(row, { dataTransfer: fileDrag() })).toBe(false)
    expect(fireEvent.drop(row, { dataTransfer: fileDrag() })).toBe(false)

    const root = screen.getByText('Season 2')
    expect(fireEvent.dragOver(root, { dataTransfer: fileDrag() })).toBe(false)
    expect(fireEvent.drop(root, { dataTransfer: fileDrag() })).toBe(false)
  })

  it('leaves an item drag alone', () => {
    // Moving assets and folders between rows predates this and must still work.
    const onDropItems = vi.fn()
    renderTree({ onDropItems, onDropFiles: vi.fn() })

    fireEvent.drop(screen.getByText('Episodes'), {
      dataTransfer: {
        types: ['application/json'],
        files: [],
        getData: () => JSON.stringify({ assetIds: ['a1'], folderIds: [] }),
      },
    })

    expect(onDropItems).toHaveBeenCalledWith('episodes', ['a1'], [])
  })
})

describe('which row is lit while a file is dragged over the tree', () => {
  const lit = (el: HTMLElement) => el.closest('div')!.className.includes('ring-accent/50')

  it('lights the row the region says is the target, and only that one', () => {
    // The marking is owned by the region rather than by each row agreeing with
    // the others, so "exactly one frame is lit" is a property of the state.
    renderTree({ onDropFiles: vi.fn(), fileDragTarget: 'episodes' })
    fireEvent.click(screen.getByText('Episodes'))

    expect(lit(screen.getByText('Episodes'))).toBe(true)
    expect(lit(screen.getByText('Episode 04'))).toBe(false)
  })

  it('names itself when it lets the marking go', () => {
    // Crossing between two folders raises the new row's dragenter before the
    // old row's dragleave, so a release that does not say where it came from
    // takes the marking off the row now under the pointer -- and the whole-area
    // frame comes back up beside it.
    const onFileDragOverFolder = vi.fn()
    renderTree({ onDropFiles: vi.fn(), onFileDragOverFolder })

    const row = screen.getByText('Episodes')
    fireEvent.dragEnter(row, { dataTransfer: fileDrag() })
    fireEvent.dragLeave(row, { dataTransfer: fileDrag() })

    expect(onFileDragOverFolder).toHaveBeenCalledWith('episodes')
    expect(onFileDragOverFolder).toHaveBeenLastCalledWith(null, 'episodes')
  })

  it('re-asserts the claim on every dragover', () => {
    // What lets the marking recover by itself if anything releases it early.
    const onFileDragOverFolder = vi.fn()
    renderTree({ onDropFiles: vi.fn(), onFileDragOverFolder })

    fireEvent.dragOver(screen.getByText('Episodes'), { dataTransfer: fileDrag() })

    expect(onFileDragOverFolder).toHaveBeenCalledWith('episodes')
  })
})

describe('what reaches the page behind the tree', () => {
  /** The tree inside something that refuses whatever nothing claimed. */
  function renderInPage(props: Record<string, unknown> = {}) {
    const onPage = vi.fn()
    const view = render(
      <div onDragOver={onPage} onDrop={onPage}>
        <FolderTree
          tree={tree}
          projectName="Season 2"
          currentFolderId={null}
          showTrash={false}
          onSelectFolder={() => {}}
          onShowTrash={() => {}}
          onCreateFolder={noop}
          onRenameFolder={noop}
          onDeleteFolder={noop}
          {...props}
        />
      </div>,
    )
    return { onPage, view }
  }

  it('keeps a drop a row has taken', () => {
    // Or the page would refuse the cursor over the one place it can be dropped.
    const { onPage } = renderInPage({ onDropFiles: vi.fn() })

    fireEvent.dragOver(screen.getByText('Episodes'), { dataTransfer: fileDrag() })
    fireEvent.drop(screen.getByText('Episodes'), { dataTransfer: fileDrag() })

    expect(onPage).not.toHaveBeenCalled()
  })

  it('lets a drop nothing claimed through', () => {
    // The two pixels of gap between two rows belong to the tree's own
    // background. Nothing there handles a file, so the page above has to get
    // the chance to refuse it -- otherwise the browser takes the drop and
    // navigates the tab to the file, losing the session. Found by hand in
    // Safari, on exactly that gap.
    const { onPage, view } = renderInPage({ onDropFiles: vi.fn() })
    const treeBackground = view.container.firstChild!.firstChild as Element

    fireEvent.drop(treeBackground, { dataTransfer: fileDrag() })

    expect(onPage).toHaveBeenCalled()
  })
})
