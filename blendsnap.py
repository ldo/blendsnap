#+
# This add-on script for Blender 2.9x manages revisions of a Blender
# document and associated files using an SQLite database. It adds a
# “Snapshots” submenu to the File menu, with “Load Snapshot...”
# and “Save Snapshot...” menu items; the latter can be used in place of
# the standard “Save” action, popping up a dialog asking the user for
# a descriptive message and saving the document and its dependencies as a
# new entry in the database. (You can still use the normal “Save”
# action to save a new working copy of the .blend file without
# saving a snapshot.) The “Load Snapshot...” action displays a
# popup menu listing all the versions of the document you previously
# saved to the database, and lets you choose one to replace the
# current working copy.
#
# Copyright 2021 Lawrence D’Oliveiro <ldo@geek-central.gen.nz>.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#-

import sys
import os
import enum
import time
import itertools
import errno
import shutil
import apsw as sqlite
import bpy

bl_info = \
    {
        "name" : "Blendsnap",
        "author" : "Lawrence D’Oliveiro <ldo@geek-central.gen.nz>",
        "version" : (0, 5, 0),
        "blender" : (2, 92, 0),
        "location" : "File > Snapshots",
        "description" : "manage versions of a .blend file using SQLite",
        "warning" : "",
        "wiki_url" : "",
        "tracker_url" : "",
        "category" : "System",
    }

#+
# Database stuff
#-

snapshot_suffix = ".ver"

class DB_OPEN(enum.IntEnum) :
    "database opening modes."
    READONLY = 0 # existing database, only for reading
    READWRITE = 1 # existing database, for reading and writing
    READWRITECREATE = 2 # create database if it doesn’t exist, open for reading and writing
#end DB_OPEN

def db_iter(conn, cmd, mapfn = lambda x : x) :
    "executes cmd on a new cursor from connection conn and yields the results in turn."
    for item in conn.cursor().execute(cmd) :
        yield mapfn(item)
    #end for
#end db_iter

def open_db(dbname, mode) :
    "opens the specified database file and returns a new connection. mode is DB_OPEN value."
    test_table = "snapshots"
    test_field = "id"
    create_tables = \
        [
            "create table snapshots\n"
            "  (\n"
            "    id integer primary key autoincrement,\n"
            "    comment varchar not null,\n"
            "    timestamp double not null\n"
            "  )\n",
            "create table files\n"
            "  (\n"
            "    snapshot_id integer not null,\n" # = snapshots.id
            "    path varchar not null,\n"
            "    timestamp double not null,\n"
            "    contents blob not null,\n"
            "    primary key (snapshot_id, path)\n"
            "  )\n",
        ]
    result = \
        sqlite.Connection \
          (
            dbname,
            flags =
                    (sqlite.SQLITE_OPEN_READONLY, sqlite.SQLITE_OPEN_READWRITE)
                        [mode >= DB_OPEN.READWRITE]
                |
                    (0, sqlite.SQLITE_OPEN_CREATE)[mode == DB_OPEN.READWRITECREATE]
          )
    cu = result.cursor()
    try :
        cu.execute("select count(*) from %s where %s is not null" % (test_table, test_field))
        inited = True
    except sqlite.SQLError :
        if mode < DB_OPEN.READWRITECREATE :
            raise
        #end if
        # assume it’s a newly-created database, need to set up the tables
        inited = False
    #end try
    if not inited :
        sys.stderr.write("Initializing new database\n") # debug
        for create_table in create_tables :
            cu.execute(create_table)
        #end for
    #end if
    return \
        result
#end open_db_common

#+
# Other useful stuff
#-

def format_compact_datetime(timestamp) :
    # returns as brief as possible a human-readable display of the specified date/time.
    then_items = time.localtime(round(timestamp))
    now = time.time()
    now_items = time.localtime(now)
    if abs(now - timestamp) < 86400 :
        format = "%H:%M:%S"
    else :
        format = "%b-%d %H:%M"
        if then_items.tm_year != now_items.tm_year :
            format = "%Y " + format
        #end if
    #end if
    return \
        time.strftime(format, then_items)
#end format_compact_datetime

def doc_saved() :
    # has the current doc been saved at least once
    return len(bpy.data.filepath) != 0
#end doc_saved

def get_db_name() :
    # name to use for the database associated with this doc
    return bpy.data.filepath + snapshot_suffix
#end get_db_name

def list_snapshots(self, context) :
    global last_snapshots_list # docs say Python must keep ref to strings
    db_name = get_db_name()
    if os.path.isfile(db_name) :
        db = open_db(db_name, DB_OPEN.READONLY)
        # Blender bug? Items in menu end up in reverse order from that in my list
        last_snapshots_list = list \
          (
            (str(entry[0]), "%s: %s" % (format_compact_datetime(entry[1]), entry[2]), "")
            for entry in db_iter(db, "select id, timestamp, comment from snapshots order by timestamp")
          )
        db.close()
    else :
        last_snapshots_list = []
    #end if
    if len(last_snapshots_list) == 0 :
        last_snapshots_list = [("", "No snapshots found", ""),]
    #end if
    return last_snapshots_list
#end list_snapshots

#+
# Mainline
#-

class LoadSnapshot(bpy.types.Operator) :
    bl_idname = "file.snapshot_load"
    bl_label = "Load Snapshot..."

    snapid : bpy.props.EnumProperty \
      (
        items = list_snapshots,
        name = "Snapshot",
        description = "which previously-saved snapshot to restore",
      )

    def draw(self, context) :
        self.layout.prop(self, "snapid")
    #end draw

    def invoke(self, context, event):
        if doc_saved() :
            result = context.window_manager.invoke_props_dialog(self)
        else :
            self.report({"ERROR"}, "Need to save the new document first")
            result = {"CANCELLED"}
        #end if
        return result
    #end invoke

    # def modal(self, context, event)
      # doesn’t seem to be needed

    def execute(self, context) :
        if len(self.snapid) != 0 :
            snapid = int(self.snapid)
            db = open_db(get_db_name(), DB_OPEN.READONLY)
            parent_dir = os.path.dirname(bpy.data.filepath)
            preserve_timestamps = False # todo -- make this an option?
            nr_restored = 0
            for childname, timestamp, contents in db_iter \
              (
                db,
                    "select files.path, files.timestamp, files.contents from snapshots"
                    " inner join files on snapshots.id = files.snapshot_id where snapshots.id = %d"
                %
                    snapid
              ) \
            :
                sys.stderr.write("restore %s\n" % childname)
                childpath = os.path.join(parent_dir, childname)
                os.makedirs(os.path.split(childpath)[0], exist_ok = True)
                childfile = open(childpath, "wb")
                childfile.write(contents)
                childfile.close()
                if preserve_timestamps :
                    os.utime(childpath, (timestamp, timestamp))
                #end if
                nr_restored += 1
            #end for
            db.close()
            sys.stderr.write("%d files restored.\n" % nr_restored)
            bpy.ops.wm.open_mainfile("EXEC_DEFAULT", filepath = bpy.data.filepath)
            result = {"FINISHED"}
        else :
            result = {"CANCELLED"}
        #end if
        return result
    #end execute

#end LoadSnapshot

class SaveSnapshot(bpy.types.Operator) :
    bl_idname = "file.snapshot_save"
    bl_label = "Save Snapshot..."

    comment : bpy.props.StringProperty(name = "Comment")

    def draw(self, context) :
        self.layout.prop(self, "comment", text = "")
    #end draw

    def invoke(self, context, event):
        if doc_saved() :
            result = context.window_manager.invoke_props_dialog(self)
        else :
            self.report({"ERROR"}, "Need to save the new document first")
            result = {"CANCELLED"}
        #end if
        return result
    #end invoke

    def execute(self, context) :

        cu = None
        snapid = snaptime = None
        parent_dir = None

        seen_filepaths = set()

        def save_file(item) :
            sys.stderr.write("save %s\n" % item)
            itempath = os.path.join(parent_dir, item)
            iteminfo = os.lstat(itempath)
            contents = open(itempath, "rb").read()
            cu.execute \
              (
                    "insert into files(snapshot_id, path, timestamp, contents) values(%d, %s, %s, %s)"
                %
                    (
                        snapid,
                        sqlite.format_sql_value(item),
                        str(iteminfo.st_mtime),
                        sqlite.format_sql_value(contents),
                    )
              )
        #end save_file

        def process_item(item) :
            # common processing for all externally-referenceable item types
            # other than nodes.
            if item.filepath not in seen_filepaths :
                seen_filepaths.add(item.filepath)
                filepath = item.filepath[2:] # relative to .blend file
                save_file(filepath)
            #end if
        #end process_item

        def process_node(node) :
            # looks for externally-referenced OSL scripts and IES parameters.
            if node.node_tree != None :
                for subnode in node.node_tree.nodes :
                    if subnode.type == "GROUP" :
                        # multiple references to a node group don’t matter,
                        # since process_item (above) automatically skips
                        # filepaths it has already seen.
                        process_node(subnode)
                    elif (
                            isinstance
                              (
                                subnode,
                                (bpy.types.ShaderNodeScript, bpy.types.ShaderNodeTexIES)
                              )
                        and
                            subnode.mode == "EXTERNAL"
                    ) :
                        process_item(subnode)
                    #end if
                #end for
            #end if
        #end process_node

    #begin execute
        db = open_db(get_db_name(), DB_OPEN.READWRITECREATE)
        bpy.ops.wm.save_as_mainfile("EXEC_DEFAULT", filepath = bpy.data.filepath)
        parent_dir = os.path.dirname(bpy.data.filepath)
        cu = db.cursor()
        cu.execute("begin transaction")
        snaptime = time.time()
        cu.execute \
          (
                "insert into snapshots(comment, timestamp) values(%s, %s)"
            %
                (sqlite.format_sql_value(self.comment), str(snaptime))
          )
        snapid, = list(db_iter
          (
            db,
            "select last_insert_rowid()",
            mapfn = lambda x : x[0]
          ))
        save_file(os.path.basename(bpy.data.filepath))
        for \
            category, match, mismatch \
        in \
            (
                ("fonts", {}, (("filepath", "<builtin>"),)),
                ("images", {"type" : "IMAGE"}, ()),
                ("libraries", {}, ()),
                ("sounds", {}, ()),
            ) \
        :
            for item in getattr(bpy.data, category) :
                if (
                        item.packed_file == None
                          # not packed into .blend file
                    and
                        item.filepath.startswith("//")
                          # must be relative to .blend file
                    and
                        not item.filepath.startswith("//..")
                          # must not be at higher level than .blend file
                    and
                        not any(getattr(item, k) == v for k, v in mismatch)
                    and
                        all(getattr(item, k) == match[k] for k in match)
                ) :
                    process_item(item)
                #end if
            #end for
        #end for
        for item in itertools.chain(bpy.data.materials, bpy.data.lights) :
            process_node(item)
        #end for
        for light in bpy.data.lights :
            process_node(light)
        #end for
        cu.execute("end transaction")
        cu.close()
        db.close()
        result = {"FINISHED"}
        return result
    #end execute

#end SaveSnapshot

class SnapshotMenu(bpy.types.Menu) :
    bl_idname = "file.snapshots_menu"
    bl_label = "Snapshots"

    def draw(self, context) :
        for op in (LoadSnapshot, SaveSnapshot) :
            self.layout.operator(op.bl_idname, text = op.bl_label)
        #end for
    #end draw

#end SnapshotMenu

def add_invoke_item(self, context) :
    self.layout.menu(SnapshotMenu.bl_idname)
#end add_invoke_item

_classes_ = \
    (
        LoadSnapshot,
        SaveSnapshot,
        SnapshotMenu,
    )

def register() :
    for ċlass in _classes_ :
        bpy.utils.register_class(ċlass)
    #end for
    bpy.types.TOPBAR_MT_file.append(add_invoke_item)
#end register

def unregister() :
    bpy.types.TOPBAR_MT_file.remove(add_invoke_item)
    for ċlass in _classes_ :
        bpy.utils.unregister_class(ċlass)
    #end for
#end unregister

if __name__ == "__main__" :
    register()
#end if
