import { create } from 'zustand'
import { IProvider, IResponse } from '@/types'
import {
  addProvider,
  deleteProviderById,
  getProviderById,
  getProviderList,
  updateProviderById,
} from '@/services/model.ts'

interface ProviderStore {
  provider: IProvider[]
  setProvider: (provider: IProvider) => void
  setAllProviders: (providers: IProvider[]) => void
  getProviderById: (id: number) => IProvider | undefined
  getProviderList: () => IProvider[]
  fetchProviderList: () => Promise<void>
  loadProviderById: (id: string) => Promise<void>
  addNewProvider: (provider: IProvider) => Promise<void>
  updateProvider: (provider: IProvider) => Promise<void>
  deleteProvider: (id: string) => Promise<void>
}

export const useProviderStore = create<ProviderStore>((set, get) => ({
  provider: [],

  // 添加或更新一个 provider
  setProvider: newProvider =>
    set(state => {
      const exists = state.provider.find(p => p.id === newProvider.id)
      if (exists) {
        return {
          provider: state.provider.map(p => (p.id === newProvider.id ? newProvider : p)),
        }
      } else {
        return { provider: [...state.provider, newProvider] }
      }
    }),

  // 设置整个 provider 列表
  setAllProviders: providers => set({ provider: providers }),
  loadProviderById: async (id: string) => {
    const res:IResponse<IProvider> = await getProviderById(id)

      const item = res
      return {
        id: item.id,
        name: item.name,
        logo: item.logo,
        apiKey: item.api_key,
        baseUrl: item.base_url,
        type: item.type,
        apiFormat: (item as Record<string, unknown>).api_format as string | undefined,
        enabled: item.enabled,
      }

  },
  addNewProvider: async (provider: IProvider) => {
    const payload = {
      ...provider,
      api_key: provider.apiKey,
      base_url: provider.baseUrl,
      api_format: provider.apiFormat || 'chat',
    }
    try {
      const res = await addProvider(payload)
      if (res.data.code === 0) {
        const item = res.data.data
        console.log('Provider ', item)

        await get().fetchProviderList()
        return  item
      }
    } catch (error) {
      console.error('Error fetching provider:', error)
    }
  },
  // 按 id 获取单个 provider
  getProviderById: id => get().provider.find(p => p.id === id),
  updateProvider: async (provider: IProvider) => {
    try {
      const existing = get().provider.find(p => p.id === provider.id)
      const merged = { ...existing, ...provider }

      const data = {
        ...merged,
        api_key: merged.apiKey,
        base_url: merged.baseUrl,
        api_format: merged.apiFormat || 'chat',
      }
      // 拦截器已解包：成功时直接返回 data 部分
      await updateProviderById(data)
      await get().fetchProviderList()
    } catch (error) {
      console.error('Error updating provider:', error)
    }
  },
  getProviderList: () => get().provider,
  deleteProvider: async (id: string) => {
    await deleteProviderById(id)
    // 后端级联清掉了该供应商下模型，本地整行移除即可
    set(state => ({ provider: state.provider.filter(p => String(p.id) !== String(id)) }))
  },
  fetchProviderList: async () => {
    try {
      const res  = await getProviderList()

        set({
          provider: res.map(
            (item: {
              id: string
              name: string
              logo: string
              api_key: string
              base_url: string
              type: string
              enabled: number
            }) => {
              return {
                id: item.id,
                name: item.name,
                logo: item.logo,
                apiKey: item.api_key,
                baseUrl: item.base_url,
                type: item.type,
                enabled: item.enabled,
              }
            }
          ),
        })
    } catch (error) {
      console.error('Error fetching provider list:', error)
    }
  },
}))
