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
