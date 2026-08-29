/** 资产 API。 */

import { api } from './client'
import type { Asset, AssetListResult } from '../types'

export const listAssets = (): Promise<AssetListResult> => api('/assets')

export interface CreateAssetInput {
  asset_id: string
  asset_no: string
  asset_type: string
  name?: string
  hostname?: string
  department?: string
  owner_user_id?: string
}

export function createAsset(input: CreateAssetInput): Promise<Asset> {
  return api('/assets', { method: 'POST', body: JSON.stringify(input) })
}

export interface UpdateAssetPatch {
  name?: string | null
  hostname?: string | null
  department?: string | null
  owner_user_id?: string | null
}

export function updateAsset(assetId: string, patch: UpdateAssetPatch): Promise<Asset> {
  return api(`/assets/${assetId}`, { method: 'PATCH', body: JSON.stringify(patch) })
}

export function deleteAsset(assetId: string): Promise<void> {
  return api(`/assets/${assetId}`, { method: 'DELETE' })
}
