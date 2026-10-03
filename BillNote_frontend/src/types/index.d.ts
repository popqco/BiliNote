export interface IProvider {
  id: string
  name: string
  logo: string
  type: string
  apiKey: string
  baseUrl: string
  /** 供应商 API 协议：'chat'（Chat Completions，默认）| 'responses'（OpenAI Responses，OpenCode 系网关等） */
  apiFormat?: string
  enabled: number
}
export interface IResponse<T> {
  code: number
  data:T
  msg: string
}