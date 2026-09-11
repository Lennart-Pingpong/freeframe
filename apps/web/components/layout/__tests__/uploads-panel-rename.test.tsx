/**
 * The rename as it is operated: the name is a target you can click, and the
 * pencil beside it is the part that says so.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// Leaving the Active tab switches on the panel's infinite scroll, which jsdom
// has no implementation for.
vi.stubGlobal(
  'IntersectionObserver',
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
)
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('@/lib/api', () => ({
  api: { post: vi.fn(), get: vi.fn().mockResolvedValue([]), patch: vi.fn() },
}))
vi.mock('swr', () => ({ mutate: vi.fn() }))

import { api } from '@/lib/api'
import { UploadsPanel } from '../uploads-panel'
import { useUploadStore, type UploadFile } from '@/stores/upload-store'

function row(patch: Partial<UploadFile> = {}): UploadFile {
  return {
    id: 'row-1',
    fileName: 'A047C012.mov',
    fileSize: 1024,
    fileType: 'video/quicktime',
    projectId: 'p1',
    projectName: 'Season 2',
    assetName: 'A047C012',
    progress: 40,
    processingProgress: 0,
    status: 'uploading',
    assetId: 'a1',
    createdAt: Date.now(),
    ...patch,
  } as UploadFile
}

function open(...files: UploadFile[]) {
  useUploadStore.setState({
    files,
    panelOpen: true,
    historyLoaded: true,
    historyHasMore: false,
  })
  return render(<UploadsPanel />)
}

const field = () => screen.getByLabelText('Asset name') as HTMLInputElement

describe('renaming from the uploads panel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.patch).mockResolvedValue({})
  })

  it('opens the field when the name itself is clicked', () => {
    open(row())

    fireEvent.click(screen.getByRole('button', { name: 'A047C012' }))

    expect(field().value).toBe('A047C012')
  })

  it('opens the same field from the pencil', () => {
    open(row())

    fireEvent.click(screen.getByRole('button', { name: 'Rename A047C012' }))

    expect(field().value).toBe('A047C012')
  })

  it('saves on Enter', async () => {
    open(row())
    fireEvent.click(screen.getByRole('button', { name: 'A047C012' }))

    fireEvent.change(field(), { target: { value: 'Ep04 Rohschnitt' } })
    fireEvent.keyDown(field(), { key: 'Enter' })

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/assets/a1', { name: 'Ep04 Rohschnitt' }),
    )
    expect(screen.getByRole('button', { name: 'Ep04 Rohschnitt' })).toBeTruthy()
  })

  it('saves when the field is left', async () => {
    open(row())
    fireEvent.click(screen.getByRole('button', { name: 'A047C012' }))

    fireEvent.change(field(), { target: { value: 'Ep04' } })
    fireEvent.blur(field())

    await waitFor(() => expect(api.patch).toHaveBeenCalledWith('/assets/a1', { name: 'Ep04' }))
  })

  it('discards the edit on Escape', async () => {
    open(row())
    fireEvent.click(screen.getByRole('button', { name: 'A047C012' }))

    fireEvent.change(field(), { target: { value: 'Ep04' } })
    fireEvent.keyDown(field(), { key: 'Escape' })

    await waitFor(() => expect(screen.getByRole('button', { name: 'A047C012' })).toBeTruthy())
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('sends one request per edit', async () => {
    open(row())
    fireEvent.click(screen.getByRole('button', { name: 'A047C012' }))

    fireEvent.change(field(), { target: { value: 'Ep04' } })
    fireEvent.keyDown(field(), { key: 'Enter' })

    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1))
  })

  it('shows why a refused rename snapped back', async () => {
    vi.mocked(api.patch).mockRejectedValue(new Error('Asset not found'))
    open(row())
    fireEvent.click(screen.getByRole('button', { name: 'A047C012' }))

    fireEvent.change(field(), { target: { value: 'Ep04' } })
    fireEvent.keyDown(field(), { key: 'Enter' })

    await waitFor(() => expect(screen.getByText('Asset not found')).toBeTruthy())
    expect(screen.getByRole('button', { name: 'A047C012' })).toBeTruthy()
  })
})

describe('a row that has not reached the server yet', () => {
  beforeEach(() => vi.clearAllMocks())

  it('is offered the rename as well', () => {
    // The pencil is there from the moment the row appears. There is no asset
    // to PATCH while it says "Queued", but the row is what initiate reads for
    // the name, so the edit is not lost -- it is just not a request yet.
    open(row({ status: 'pending', assetId: undefined }))

    fireEvent.click(screen.getByRole('button', { name: 'Rename A047C012' }))
    fireEvent.change(field(), { target: { value: 'Ep04' } })
    fireEvent.keyDown(field(), { key: 'Enter' })

    expect(screen.getByRole('button', { name: 'Ep04' })).toBeTruthy()
    expect(api.patch).not.toHaveBeenCalled()
  })
})

describe('rows the panel does not offer a rename on', () => {
  beforeEach(() => vi.clearAllMocks())

  it('a history row, which may belong to a project the user only reviews', () => {
    // `/me/assets` is not "my uploads": with no filter it returns every asset
    // in every project the user is a member of, in whatever role, plus
    // anything merely shared or assigned to them. Most of that list is not
    // theirs to rename.
    open(row({ id: 'history-a1', fromHistory: true, status: 'complete' }))
    // A completed row is not on the Active tab the panel opens on, and a
    // control cannot be absent from a row that was never rendered.
    fireEvent.click(screen.getByRole('button', { name: /Complete/ }))

    expect(screen.getByText('A047C012')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Rename A047C012' })).toBeNull()
  })
})
