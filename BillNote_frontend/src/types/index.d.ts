export interface IProvider {
  id: string
  name: string
  logo: string
  type: string
  apiKey: string
  baseUrl: string
  /** 供应商 API 协议：'chat'（Chat Completions，默认）| 'responses'（OpenAI Responses，OpenCode 系网关等） */
  apiFormat?: string
  /** 自定义请求头（JSON 对象字符串），如 '{"x-opencode-session": "<uuid>"}' */
  extraHeaders?: string
  enabled: number
}
/** 已启用模型行；思考强度是模型级属性（不同模型档位不同），不挂在供应商上 */
export interface IModelListItem {
  id: number
  provider_id: string
  model_name: string
  /** 思考强度（reasoning effort）：''（不设置，默认）| minimal | low | medium | high | xhigh | max */
  thinking_effort?: string
  created_at?: string
}
export interface IResponse<T> {
  code: number
  data:T
  msg: string
}
