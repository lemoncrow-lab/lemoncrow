import { dataSource } from "./db";
import { User } from "./user";

export async function saveUser(name: string): Promise<void> {
  const manager = dataSource.manager;
  await manager.save(User, { name });
}

export async function removeUser(id: string): Promise<void> {
  await dataSource.manager.delete(User, { id });
}
