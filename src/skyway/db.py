# Copyright (c) 2019-2024 The University of Chicago.
# Part of skyway, released under the BSD 3-Clause License.

# Maintainer: Yuxing Peng, Trung Nguyen

from . import cfg, debug
import pymysql

class DBConnector:
    """ DBConnector provides wrapper functions for user database queries
    """
    
    """ handle to the database: class static member """
    _h = None
    
    def __init__(self):
        # exect to have an entry 'db' in $SKYWAYROTT/etc/root.yaml
        if DBConnector._h is None:
            db_config = cfg['db']
            DBConnector._h = pymysql.connect(host=db_config['host'],
                                             user=db_config['username'],
                                             password=db_config['password'],
                                             database=db_config['database'],
                                             port=db_config['port'])
        # self.cursor is the current position of the handle
        self.cursor = DBConnector._h.cursor()
        
    def exec(self, query):
        """ Executes an SQL query
        Parameter
        ---------
        query: string
          SQL query
        """
        if debug:
            print(query)

        try:
            self.cursor.execute(query)
        except Exception as e:
            raise Exception("MySQL Error: " + query + "\nException Details: " + str(e.args))
        
        DBConnector._h.commit()
        
    def update_one(self, tbl, key, ident, values):
        sets = ','.join([("%s='%s'" % (k, str(v))) for k,v in values.items()])
        self.exec("update %s set %s where %s='%s' limit 1" % (tbl, sets, key, str(ident)))
    
    def remove_one(self, tbl, key, ident):
        self.exec("delete from %s where %s='%s' limit 1" % (tbl, key, str(ident)))
        
    
    def insert_one(self, tbl, **values):
        self.exec("insert into %s (%s) values (%s)" % (tbl, ','.join(values.keys()), ','.join([("'%s'" % (str(v))) for v in values.values()])))

    def select(self, tbl, fields, **kwargs):
        """
        wrap the SELECT commmand: select `fields` from the table `tbl` with optional kwargs
        """
        query = f"select {fields} from {tbl}"
        
        if 'where' in kwargs:
            query += ' where ' + kwargs['where']
        
        if 'group' in kwargs:
            query += ' group by ' + kwargs['group']
        
        if 'order' in kwargs:
            query += ' order by ' + kwargs['order']
        
        if 'limit' in kwargs:
            query += ' limit ' + kwargs['limit']
        
        self.exec(query)
        return self.cursor.fetchall()
