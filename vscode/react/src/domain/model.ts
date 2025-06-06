import type { Column } from '@/api/client'

export interface Model {
  displayName: string
  name: string
  fqn: string
  path: string
  dialect: string
  full_path: string
  // type: ModelType
  columns: Column[]
  // default_catalog?: ModelDefaultCatalog
  // description?: ModelDescription
  // sql?: ModelSql
  // definition?: ModelDefinition
  hash: string
}
