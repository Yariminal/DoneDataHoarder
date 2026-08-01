import sqlite3
conn = sqlite3.connect(r'D:\Test\Test db\bipa_2021.db')
c = conn.cursor()
c.execute('SELECT ai_model, COUNT(*) FROM files WHERE ai_model IS NOT NULL GROUP BY ai_model')
for row in c.fetchall(): print(row)

c.execute("SELECT COUNT(*) FROM files WHERE ai_suggested_name IS NOT NULL")
print('Files with ai_suggested_name:', c.fetchone()[0])

c.execute("SELECT COUNT(*) FROM files WHERE ai_description LIKE '%inference failed%'")
print('Files with inference failed:', c.fetchone()[0])

conn.close()
