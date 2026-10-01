import time
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

pool: ConnectionPool | None = None


# 幂等的建表脚本：服务首次启动时自动初始化全部结构
# 说明：排他约束中 text 列显式使用 GiST 的 gist_bbox_ops 操作符类（标量等值），
# 因此不需要 contrib 的 btree_gist 扩展。
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS works (
    code       text PRIMARY KEY,
    title      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rightsholders (
    code       text PRIMARY KEY,
    name       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 分成协议：半开区间 [valid_from, valid_to)，valid_to 为空表示长期有效
-- 区间不重叠由应用层在"每作品事务咨询锁 + OVERLAPS 检测"下强制（无需 contrib 扩展）
CREATE TABLE IF NOT EXISTS agreements (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_code  text NOT NULL REFERENCES works(code),
    valid_from timestamptz NOT NULL,
    valid_to   timestamptz,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agreements_range_chk
        CHECK (valid_to IS NULL OR valid_to > valid_from)
);
-- 作品 + 生效时间的匹配查询索引
CREATE INDEX IF NOT EXISTS agreements_work_from_idx
    ON agreements(work_code, valid_from);

CREATE TABLE IF NOT EXISTS agreement_shares (
    agreement_id bigint NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    holder_code  text NOT NULL REFERENCES rightsholders(code),
    percent      numeric(8,4) NOT NULL CHECK (percent > 0 AND percent <= 100),
    PRIMARY KEY (agreement_id, holder_code)
);

CREATE OR REPLACE FUNCTION check_shares_total() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    total numeric(12,4);
    aid   bigint;
BEGIN
    aid := COALESCE(NEW.agreement_id, OLD.agreement_id);
    SELECT sum(percent) INTO total FROM agreement_shares WHERE agreement_id = aid;
    IF total IS DISTINCT FROM 100.0000 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'agreement shares must total 100 percent, got '
                      || COALESCE(total::text, '0');
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS agreement_shares_total_trg ON agreement_shares;
-- 延迟约束触发器：允许先插协议、再逐条插分成，事务提交时校验合计 100%
CREATE CONSTRAINT TRIGGER agreement_shares_total_trg
    AFTER INSERT OR UPDATE OR DELETE ON agreement_shares
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_shares_total();

-- 播放记录：import_id 为调用方提供的唯一编号
CREATE TABLE IF NOT EXISTS plays (
    import_id   text PRIMARY KEY,
    work_code   text NOT NULL REFERENCES works(code),
    occurred_at timestamptz NOT NULL,
    amount      bigint NOT NULL CHECK (amount >= 0),
    corrects_id text UNIQUE REFERENCES plays(import_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT plays_no_self_corr CHECK (corrects_id IS NULL OR corrects_id <> import_id)
);
CREATE INDEX IF NOT EXISTS plays_occurred_idx ON plays(occurred_at);
-- corrects_id 自带 UNIQUE：同一记录最多被更正一次 => 禁止分叉

-- 结算单据
CREATE TABLE IF NOT EXISTS statement_batches (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_start         timestamptz NOT NULL,
    period_end           timestamptz NOT NULL,
    kind                 text NOT NULL CHECK (kind IN ('initial', 'supplement')),
    supersedes_batch_id  bigint REFERENCES statement_batches(id),
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT statement_batches_period_chk CHECK (period_end > period_start)
);
-- 同一期间只能有一张初次单（追补单数量不限）
CREATE UNIQUE INDEX IF NOT EXISTS statement_batches_initial_uniq
    ON statement_batches(period_start, period_end)
    WHERE kind = 'initial';

-- 每次出具时待处理播放的快照
CREATE TABLE IF NOT EXISTS statement_pending (
    period_start timestamptz NOT NULL,
    period_end   timestamptz NOT NULL,
    import_id    text NOT NULL REFERENCES plays(import_id),
    work_code    text NOT NULL,
    occurred_at  timestamptz NOT NULL,
    amount       bigint NOT NULL,
    reason       text NOT NULL,
    PRIMARY KEY (period_start, period_end, import_id)
);

-- 单据明细行：初次单为当期应付，追补单为差额（可正可负）
CREATE TABLE IF NOT EXISTS statement_lines (
    batch_id    bigint NOT NULL REFERENCES statement_batches(id) ON DELETE CASCADE,
    holder_code text NOT NULL,
    amount      bigint NOT NULL,
    PRIMARY KEY (batch_id, holder_code)
);
CREATE INDEX IF NOT EXISTS statement_lines_holder_idx ON statement_lines(holder_code);

-- 单据逐笔归因证据：出具时冻结，此后不再随播放/协议数据变化。
-- 按更正链"原始编号"(root_import_id) 归集；change_type 标注证据的性质：
--   added   = 新增（初次单的全部证据、追补单中出现/重算后的链）
--   removed = 撤销（跨期更正使旧链尾失效，或协议后补改变逐笔结果时冲销旧证据，金额为负）
--   legacy  = 无逐笔证据的历史旧账差额（不指向任何播放，绝不用当前播放冒充历史）
CREATE TABLE IF NOT EXISTS statement_attributions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id        bigint NOT NULL REFERENCES statement_batches(id) ON DELETE CASCADE,
    holder_code     text NOT NULL,
    root_import_id  text,                 -- legacy 行无对应播放，允许为空
    change_type     text NOT NULL CHECK (change_type IN ('added', 'removed', 'legacy')),
    -- 出具时冻结的链与分摊快照
    tail_import_id  text,
    work_code       text,
    occurred_at     timestamptz,
    agreement_id    bigint,
    tail_amount     bigint,
    share_percent   numeric(8,4),
    amount          bigint NOT NULL,      -- 逐笔分摊额（removed 为负）
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- 同一单据同一权利人同一链同一性质只能有一条（并发出具不会重复生成证据）。
    -- legacy 行 root_import_id 为空，用 COALESCE 归入同一命名空间；
    -- 表达式只能建唯一索引（表级 UNIQUE 约束不支持表达式）。
    CONSTRAINT statement_attributions_play_fields_chk CHECK (
        change_type = 'legacy'
        OR (root_import_id IS NOT NULL AND tail_import_id IS NOT NULL
            AND work_code IS NOT NULL AND occurred_at IS NOT NULL
            AND agreement_id IS NOT NULL AND tail_amount IS NOT NULL
            AND share_percent IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS statement_attributions_uniq
    ON statement_attributions(
        batch_id, holder_code, COALESCE(root_import_id, ''), change_type
    );
-- 按单据 + 权利人分页/合计的主查询索引（同一次查询内按链、性质稳定排序）
CREATE INDEX IF NOT EXISTS statement_attributions_batch_holder_idx
    ON statement_attributions(batch_id, holder_code, root_import_id, change_type, id);
-- 历史不可变：证据一经插入即禁止修改/删除（级联删除单据除外）
CREATE OR REPLACE FUNCTION statement_attributions_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'insufficient_privilege',
        MESSAGE = 'statement attribution evidence is frozen and must not be '
                  || TG_OP || 'd';
END;
$$;
DROP TRIGGER IF EXISTS statement_attributions_frozen_trg ON statement_attributions;
CREATE TRIGGER statement_attributions_frozen_trg
    BEFORE UPDATE OR DELETE ON statement_attributions
    FOR EACH ROW EXECUTE FUNCTION statement_attributions_frozen();

-- 已登记付款：财务凭外部付款流水号登记，金额为正整数（最小货币单位）。
-- external_ref 全局唯一：同一流水号同内容重试幂等返回原记录，异内容冲突拒绝；
-- 付款一经登记不可修改/删除（跨期更正产生负额追补时也不撤销历史付款）。
CREATE TABLE IF NOT EXISTS payments (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    holder_code  text NOT NULL REFERENCES rightsholders(code),
    external_ref text NOT NULL UNIQUE,
    amount       bigint NOT NULL CHECK (amount > 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);
-- 按权利人列付款流水（对账单查询主索引）
CREATE INDEX IF NOT EXISTS payments_holder_idx ON payments(holder_code, id);

CREATE OR REPLACE FUNCTION payments_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'insufficient_privilege',
        MESSAGE = 'registered payments are frozen and must not be ' || TG_OP
                  || 'd; negative supplements never revoke historical payments';
END;
$$;
DROP TRIGGER IF EXISTS payments_frozen_trg ON payments;
CREATE TRIGGER payments_frozen_trg
    BEFORE UPDATE OR DELETE ON payments
    FOR EACH ROW EXECUTE FUNCTION payments_frozen();

-- 银行退票：财务收到银行退回时，凭"原付款流水号 + 退回流水号 + 正整数分额"登记。
-- refund_ref（退回流水号）全局唯一：同内容重试幂等返回原记录，异内容冲突拒绝；
-- 原付款与单据保持不可变，退票只追加本记录（一经登记不可修改/删除）。
-- 余额口径：单据正负累计 - 付款累计 + 退回累计（退回加回权利人余额，可为负）。
CREATE TABLE IF NOT EXISTS payment_refunds (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id   bigint NOT NULL REFERENCES payments(id),
    refund_ref   text NOT NULL UNIQUE,
    amount       bigint NOT NULL CHECK (amount > 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);
-- 按原付款汇总累计退回（超额校验主索引）；按权利人列退回流水走 payments 关联
CREATE INDEX IF NOT EXISTS payment_refunds_payment_idx
    ON payment_refunds(payment_id, id);

-- 兜底约束：同一原付款的累计退回不得超过原付款金额（应用层在行锁下先行校验，
-- 触发器为纵深防御；付款行锁使并发退回串行，正常路径不会触发）。
CREATE OR REPLACE FUNCTION payment_refunds_within_payment() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    paid     bigint;
    refunded bigint;
BEGIN
    SELECT amount INTO paid FROM payments WHERE id = NEW.payment_id;
    SELECT COALESCE(SUM(amount), 0) INTO refunded
    FROM payment_refunds
    WHERE payment_id = NEW.payment_id AND id IS DISTINCT FROM NEW.id;
    IF refunded + NEW.amount > paid THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'cumulative refunds ' || (refunded + NEW.amount)
                      || ' exceed original payment amount ' || paid;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS payment_refunds_cap_trg ON payment_refunds;
CREATE TRIGGER payment_refunds_cap_trg
    BEFORE INSERT ON payment_refunds
    FOR EACH ROW EXECUTE FUNCTION payment_refunds_within_payment();

CREATE OR REPLACE FUNCTION payment_refunds_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'insufficient_privilege',
        MESSAGE = 'registered refunds are frozen and must not be ' || TG_OP
                  || 'd; original payments and statements stay immutable';
END;
$$;
DROP TRIGGER IF EXISTS payment_refunds_frozen_trg ON payment_refunds;
CREATE TRIGGER payment_refunds_frozen_trg
    BEFORE UPDATE OR DELETE ON payment_refunds
    FOR EACH ROW EXECUTE FUNCTION payment_refunds_frozen();

-- 付款额度预留指令（财务"先预留、后确认付款"两阶段）：
-- 财务凭权利人编号 holder_code、调用方唯一指令号 instruction_ref 与
-- 正整数分额 amount 预留付款额度；仅当"当前余额 - 全部待确认预留"仍足额
-- 时才创建 pending 指令。
--   pending   待确认：占用预留额度，可取消（释放额度）或确认（原子登记付款）
--   cancelled 已取消：终态，额度已释放，不可再改
--   confirmed 已确认：终态，已原子登记一笔 payments 付款（payment_id 绑定）
-- instruction_ref 全局唯一：同号同内容重试幂等返回原指令，同号异内容拒绝（409）。
-- 已确认指令的 external_ref 为该笔付款的外部流水号：同流水号确认重试幂等，
-- 异流水号或流水号已被其他付款占用拒绝（409）。
CREATE TABLE IF NOT EXISTS payment_instructions (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    holder_code     text NOT NULL REFERENCES rightsholders(code),
    instruction_ref text NOT NULL UNIQUE,
    amount          bigint NOT NULL CHECK (amount > 0),
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'cancelled', 'confirmed')),
    -- 确认时提交的外部付款流水号；仅 confirmed 非空，全局绑定到一笔付款
    external_ref    text,
    payment_id      bigint REFERENCES payments(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    confirmed_at    timestamptz,
    cancelled_at    timestamptz,
    CONSTRAINT payment_instructions_pending_fields_chk CHECK (
        status <> 'pending'
        OR (external_ref IS NULL AND payment_id IS NULL
            AND confirmed_at IS NULL AND cancelled_at IS NULL)
    ),
    CONSTRAINT payment_instructions_cancelled_fields_chk CHECK (
        status <> 'cancelled'
        OR (external_ref IS NULL AND payment_id IS NULL
            AND confirmed_at IS NULL AND cancelled_at IS NOT NULL)
    ),
    CONSTRAINT payment_instructions_confirmed_fields_chk CHECK (
        status <> 'confirmed'
        OR (external_ref IS NOT NULL AND payment_id IS NOT NULL
            AND confirmed_at IS NOT NULL AND cancelled_at IS NULL)
    )
);
-- 权利人全部待确认预留汇总（创建预留与直接付款的"避开预留"校验主索引）；
-- 确认时"更早创建的待确认指令"按创建次序（id 升序）扫描。
CREATE INDEX IF NOT EXISTS payment_instructions_holder_pending_idx
    ON payment_instructions(holder_code, id)
    WHERE status = 'pending';
-- 确认时按外部流水号反查已确认指令（同流水号确认幂等 / 异流水号拒绝）
CREATE UNIQUE INDEX IF NOT EXISTS payment_instructions_confirmed_ref_idx
    ON payment_instructions(external_ref)
    WHERE status = 'confirmed';

-- 终态与字段守卫：pending 只允许迁移到 cancelled/confirmed（由应用层在持锁
-- 事务内完成，同时改写对应时间/付款字段）；cancelled/confirmed 为终态，
-- 任何 UPDATE/DELETE 都被拒绝（历史指令与付款不可改写）。
CREATE OR REPLACE FUNCTION payment_instructions_guarded() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'insufficient_privilege',
            MESSAGE = 'payment instructions must not be deleted; history is immutable';
    END IF;
    IF OLD.status IN ('cancelled', 'confirmed')
       OR NEW.status NOT IN ('pending', 'cancelled', 'confirmed')
       OR NEW.instruction_ref IS DISTINCT FROM OLD.instruction_ref
       OR NEW.holder_code    IS DISTINCT FROM OLD.holder_code
       OR NEW.amount         IS DISTINCT FROM OLD.amount
       OR NEW.id             IS DISTINCT FROM OLD.id
       OR NEW.created_at     IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION USING
            ERRCODE = 'insufficient_privilege',
            MESSAGE = 'payment instruction ' || OLD.status
                      || ' is terminal and content must not change';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS payment_instructions_guarded_trg ON payment_instructions;
CREATE TRIGGER payment_instructions_guarded_trg
    BEFORE UPDATE OR DELETE ON payment_instructions
    FOR EACH ROW EXECUTE FUNCTION payment_instructions_guarded();

-- payments 侧反绑：一笔已登记付款至多绑定一条确认指令；确认预留之外的
-- 直接登记付款 payment_id 不出现于本表（instruction_id 为空）。
ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS instruction_id bigint REFERENCES payment_instructions(id);
CREATE UNIQUE INDEX IF NOT EXISTS payments_instruction_uniq
    ON payments(instruction_id)
    WHERE instruction_id IS NOT NULL;

-- 单据的可追溯性标记：traceable（本轮起出具、带逐笔证据）；
-- legacy_untraceable（升级前已存在、只有汇总明细行、无法逐笔追溯的存量单据）
ALTER TABLE statement_batches
    ADD COLUMN IF NOT EXISTS evidence_status text NOT NULL DEFAULT 'traceable';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'statement_batches_evidence_chk'
    ) THEN
        ALTER TABLE statement_batches
            ADD CONSTRAINT statement_batches_evidence_chk
            CHECK (evidence_status IN ('traceable', 'legacy_untraceable'));
    END IF;
END $$;
-- 幂等迁移：把"已有非零明细行但无任何逐笔证据"的存量单据标记为不可追溯；
-- 空单（仅有待处理播放）不算旧账，仍可追溯。
UPDATE statement_batches b
SET evidence_status = 'legacy_untraceable'
WHERE evidence_status = 'traceable'
  AND EXISTS (
      SELECT 1 FROM statement_lines l
      WHERE l.batch_id = b.id AND l.amount <> 0
  )
  AND NOT EXISTS (SELECT 1 FROM statement_attributions a WHERE a.batch_id = b.id);

-- 银行回单批次：batch_no 全局唯一；content_hash 为回单行的规范化内容哈希。
-- 同批次同内容重试幂等返回原批次；批次号异内容复用整批拒绝（409）。
CREATE TABLE IF NOT EXISTS receipt_batches (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_no     text NOT NULL UNIQUE,
    content_hash text NOT NULL,
    line_count   integer NOT NULL CHECK (line_count > 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- 银行回单：receipt_no 全局内容绑定（异内容复用整批拒绝；
-- 同内容复用不拒绝，但因流水号已被首次导入占用而标为重复回单）。
-- 跨批次同一外部付款流水号：仅首次成功导入的回单参与核验（is_duplicate=false），
-- 其余在导入时标记为重复回单（is_duplicate=true），不参与核验。
CREATE TABLE IF NOT EXISTS receipts (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id     bigint NOT NULL REFERENCES receipt_batches(id),
    receipt_no   text NOT NULL,
    external_ref text NOT NULL,
    holder_code  text NOT NULL,
    amount       bigint NOT NULL CHECK (amount > 0),
    is_duplicate boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, receipt_no)
);
-- 回单编号全局内容检查、流水号跨批次去重的主查询索引
CREATE INDEX IF NOT EXISTS receipts_no_idx ON receipts(receipt_no);
CREATE INDEX IF NOT EXISTS receipts_ref_idx ON receipts(external_ref);

-- 回单核验版本：导入时冻结第 1 版逐行结论；付款迟到后可复核，
-- 结论变化才生成新版本（version 递增），结论不变不新增版本；
-- 旧版本与原始回单始终可查。
CREATE TABLE IF NOT EXISTS receipt_verifications (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id   bigint NOT NULL REFERENCES receipt_batches(id),
    version    integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, version)
);

-- 逐行核验结论（冻结，此后不随付款数据变化）：
--   matched                 匹配（流水号命中已登记付款，权利人与金额均一致）
--   payment_not_registered  付款未登记（流水号无已登记付款）
--   mismatched              权利人不符和/或金额不符（两项均不符同时列出）
--   duplicate               重复回单（跨批次同流水号，不参与核验）
CREATE TABLE IF NOT EXISTS receipt_verification_lines (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    verification_id     bigint NOT NULL REFERENCES receipt_verifications(id) ON DELETE CASCADE,
    receipt_id          bigint NOT NULL REFERENCES receipts(id),
    status              text NOT NULL CHECK (status IN
                            ('matched', 'payment_not_registered', 'mismatched', 'duplicate')),
    holder_mismatch     boolean NOT NULL DEFAULT false,
    amount_mismatch     boolean NOT NULL DEFAULT false,
    payment_holder_code text,
    payment_amount      bigint,
    -- 冻结该版时，原付款的累计退回额（退票不改变匹配/不符判定；
    -- 导入与复核时一并冻结，退回额变化即视为结论变化、产生新版本）
    payment_refunded_total bigint NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (verification_id, receipt_id)
);

-- 幂等迁移：已建库补"冻结累计退回额"列（历史版本保持 0，旧版结论不改写）
ALTER TABLE receipt_verification_lines
    ADD COLUMN IF NOT EXISTS payment_refunded_total bigint NOT NULL DEFAULT 0;

-- 历史不可变：回单批次、原始回单、核验版本与逐行结论一经写入
-- 即禁止修改/删除（核验只新增版本，绝不改写付款、余额或单据）。
CREATE OR REPLACE FUNCTION receipts_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'insufficient_privilege',
        MESSAGE = 'receipt batches, receipts and verification results are frozen '
                  || 'and must not be ' || TG_OP || 'd';
END;
$$;
DROP TRIGGER IF EXISTS receipt_batches_frozen_trg ON receipt_batches;
CREATE TRIGGER receipt_batches_frozen_trg
    BEFORE UPDATE OR DELETE ON receipt_batches
    FOR EACH ROW EXECUTE FUNCTION receipts_frozen();
DROP TRIGGER IF EXISTS receipts_frozen_trg ON receipts;
CREATE TRIGGER receipts_frozen_trg
    BEFORE UPDATE OR DELETE ON receipts
    FOR EACH ROW EXECUTE FUNCTION receipts_frozen();
DROP TRIGGER IF EXISTS receipt_verifications_frozen_trg ON receipt_verifications;
CREATE TRIGGER receipt_verifications_frozen_trg
    BEFORE UPDATE OR DELETE ON receipt_verifications
    FOR EACH ROW EXECUTE FUNCTION receipts_frozen();
DROP TRIGGER IF EXISTS receipt_verification_lines_frozen_trg ON receipt_verification_lines;
CREATE TRIGGER receipt_verification_lines_frozen_trg
    BEFORE UPDATE OR DELETE ON receipt_verification_lines
    FOR EACH ROW EXECUTE FUNCTION receipts_frozen();
"""


async def init_pool() -> None:
    global pool
    last_error = None
    # db 容器可能尚不可用（即使有 healthcheck 也容忍竞态），重试等待
    for attempt in range(30):
        try:
            p = ConnectionPool(
                conninfo=settings.database_url,
                min_size=settings.db_min_size,
                max_size=settings.db_max_size,
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=False,
            )
            p.open(wait=True)
            with p.connection() as conn:
                conn.execute("SELECT 1")
            pool = p
            with pool.connection() as conn:
                conn.execute(SCHEMA_SQL)
            return
        except Exception as exc:  # pragma: no cover - startup retry
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"cannot connect to database: {last_error}")


async def close_pool() -> None:
    if pool is not None:
        pool.close()


@asynccontextmanager
async def conn_cm():
    assert pool is not None
    with pool.connection() as conn:
        yield conn


def get_conn():
    """FastAPI 依赖：从连接池取连接（autocommit；写操作显式开事务）。

    连接上下文在退出时自动回滚未提交事务并归还连接池。
    """
    assert pool is not None
    with pool.connection() as conn:
        yield conn
