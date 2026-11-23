import { dataSource } from "./db";
import { User } from "./user";

export async function saveUser(name: string): Promise<void> {
  const manager = dataSource.manager;
  await manager.save(User, { name });
}

export async function removeUser(id: string): Promise<void> {
  await dataSource.manager.delete(User, { id });
}

export async function renameUser(id: string, name: string): Promise<void> {
  const user = await dataSource.manager.findOne(User, { where: { id } });
  if (user) {
    user.name = name;
    await dataSource.manager.save(user);
  }
}
