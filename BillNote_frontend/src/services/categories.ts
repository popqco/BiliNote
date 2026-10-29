import request from '@/utils/request'

/** 笔记分类（单选）：一篇笔记属于一个分类，无赋值 = 未分类。
 *
 * 分类是后端独立 JSON 元数据（config/note_categories.json），不影响笔记正文；
 * 删除分类只断开关联，笔记本身不动。
 */
export interface NoteCategory {
  id: string
  name: string
  color: string
  created_at: number
}

export const get_note_categories = async (): Promise<{ categories: NoteCategory[] }> => {
  return await request.get('/note_categories')
}

export const create_note_category = async (name: string): Promise<NoteCategory> => {
  return await request.post('/note_categories/create', { name })
}

export const rename_note_category = async (id: string, name: string): Promise<NoteCategory> => {
  return await request.post('/note_categories/rename', { id, name })
}

export const delete_note_category = async (
  id: string,
): Promise<{ removed_assignments: number }> => {
  return await request.post('/note_categories/delete', { id })
}

/** 批量归类；category_id=null 表示设为未分类。 */
export const assign_note_category = async (
  task_ids: string[],
  category_id: string | null,
): Promise<{ updated: number }> => {
  return await request.post('/note_categories/assign', { task_ids, category_id })
}
