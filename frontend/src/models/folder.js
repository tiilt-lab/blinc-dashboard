export class FolderModel {
    // Server Fields
    id;
    name;
    owner_id;
    creation_date;
    parent;
    // The owner's email (sent for any folder that is not the caller's own) and
    // whether it is theirs.
    owner;
    owned;
    // The caller's level here — "viewer", "editor" or "manager" — and whether
    // they may change who has access (managers, and admins on any folder).
    access;
    can_manage_members;

    static fromJson(json) {
        const model = new FolderModel();
        model.id = json['id'];
        model.name = json['name'];
        model.creation_date = new Date(json['creation_date']);
        model.parent = json['parent'];
        model.owner = json['owner'] != null ? json['owner'] : null;
        model.owned = json['owned'] !== false;
        model.access = json['access'] || 'manager';
        model.can_manage_members = json['can_manage_members'] !== false;
        return model;
    }

    // The folders a person may file sessions or folders into, for pickers:
    // view-only shared folders drop out (admins may file anywhere), and any
    // folder whose parent dropped out is lifted to the top so it stays
    // reachable in the tree.
    static fileable(folders, me) {
        const role = (me || {}).role;
        if (role === 'admin' || role === 'super') return folders;
        const kept = folders.filter((f) => f.access !== 'viewer');
        const ids = new Set(kept.map((f) => f.id));
        return kept.map((f) => {
            if (f.parent == null || ids.has(f.parent)) return f;
            return Object.assign(Object.create(FolderModel.prototype), f, { parent: null });
        });
    }

    // Converts JSON to FolderModel[]
    static fromJsonList(jsonArray){
      const folders = [];
      for (const el of jsonArray) {
        folders.push(FolderModel.fromJson(el));
      }
        return folders;
    }
}
