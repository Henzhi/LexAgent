"""回填 document_chunks.metadata 的 law_name / article_range。

问题：经 IngestionPipeline 入库的文档（含 LawData 批量导入）其 chunk 的
metadata 未写入 law_name / article_range，导致检索结果不显示「引用条文」。
本脚本直接更新已有数据的 metadata JSONB（无需重新 embedding）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def main():
    conn = psycopg2.connect(os.environ["PG_CONN"])
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE document_chunks dc
        SET metadata = jsonb_set(
            jsonb_set(
                COALESCE(dc.metadata, '{}'::jsonb),
                '{law_name}', to_jsonb(d.title::text)
            ),
            '{article_range}',
            to_jsonb(COALESCE(substring(dc.content FROM '第[一二三四五六七八九十百千零两0-9]+条'), '')::text)
        )
        FROM documents d
        WHERE d.id = dc.doc_id
          AND (dc.metadata ->> 'law_name' IS NULL OR dc.metadata ->> 'law_name' = '')
        """
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    print(f"已更新 chunk 数: {n}")


if __name__ == "__main__":
    main()
