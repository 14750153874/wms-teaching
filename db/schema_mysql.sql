-- =============================================================================
-- 智能仓库管理系统（WMS）数据库建表脚本  V5.0（电商通用 · 订单驱动 · 含盘点/调拨/成本/效期）
-- 数据库：MySQL 8.0    字符集：utf8mb4 / utf8mb4_general_ci    时区：Asia/Shanghai
--
-- 约定：
--   1. 表名前缀：sys_（系统权限） / base_（基础资料） / wms_（仓储作业） / ec_（电商域）
--   2. 主键统一 bigint unsigned auto_increment；不使用物理外键（引用完整性由应用层与校验脚本保证）
--   3. 业务表含 create_by / create_time / update_by / update_time / is_deleted
--   4. 数量 decimal(18,3)，金额与单价 decimal(18,4)，禁止 float/double
--   5. 库存并发控制：wms_inventory 的 available_qty + version 乐观锁 + 条件更新
--   6. 过账幂等：单据 request_id 唯一索引 + wms_inventory_txn.uk_biz_detail 唯一索引
--   7. 流水来源必须区分 biz_type：电商发货=21、电商退货=22，绝不能复用出库单(4)/入库单(1/2)，
--      否则不同来源表的自增 ID 会在 uk_biz_detail(biz_type,biz_id,detail_id) 上相撞，
--      导致第二条真实流水被当作重复丢弃（详见电商化改进方案_V4.md 6.3）。
--   8. 本脚本共 40 张表 = V2 的 27 张 + V4 电商域 12 张 + V5 任务日志 1 张。
--      存量库升级见文件末尾「V4 增量升级（存量库）」。
-- 执行顺序：先本脚本，再由应用首次启动时执行初始化数据（管理员账号密码由程序生成并打印一次）
-- =============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- -----------------------------------------------------------------------------
-- 1. 系统与权限
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `sys_user`;
CREATE TABLE `sys_user` (
  `id`              bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  `username`        varchar(64)  NOT NULL COMMENT '登录名',
  `password`        varchar(100) NOT NULL COMMENT 'BCrypt 密文（strength=10）',
  `real_name`       varchar(64)  NOT NULL COMMENT '姓名',
  `phone`           varchar(20)  DEFAULT NULL COMMENT '手机号（展示脱敏）',
  `status`          tinyint(1)   NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  `pwd_update_time` datetime     DEFAULT NULL COMMENT '密码最后修改时间（90天有效期）',
  `last_login_time` datetime     DEFAULT NULL COMMENT '最后登录时间',
  `remark`          varchar(255) DEFAULT NULL,
  `create_by`       bigint unsigned DEFAULT NULL,
  `create_time`     datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`       bigint unsigned DEFAULT NULL,
  `update_time`     datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`      tinyint(1)   NOT NULL DEFAULT 0 COMMENT '0未删除 1已删除',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_username` (`username`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户';

DROP TABLE IF EXISTS `sys_role`;
CREATE TABLE `sys_role` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `role_code`   varchar(64)  NOT NULL COMMENT '角色编码',
  `role_name`   varchar(64)  NOT NULL COMMENT '角色名称',
  `data_scope`  tinyint      NOT NULL DEFAULT 3 COMMENT '1全部 2自定义仓库 3本仓库 4本仓库及下级',
  `status`      tinyint(1)   NOT NULL DEFAULT 1,
  `remark`      varchar(255) DEFAULT NULL,
  `create_by`   bigint unsigned DEFAULT NULL,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`   bigint unsigned DEFAULT NULL,
  `update_time` datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`  tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_role_code` (`role_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色';

DROP TABLE IF EXISTS `sys_permission`;
CREATE TABLE `sys_permission` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `perm_code`   varchar(80)  NOT NULL COMMENT '权限码，如 wms:out:confirm',
  `perm_name`   varchar(64)  NOT NULL COMMENT '权限名称',
  `perm_type`   tinyint      NOT NULL COMMENT '1菜单 2按钮 3接口',
  `parent_id`   bigint unsigned NOT NULL DEFAULT 0 COMMENT '上级ID（菜单树）',
  `path`        varchar(128) DEFAULT NULL COMMENT '前端路由或接口路径',
  `method`      varchar(10)  DEFAULT NULL COMMENT 'HTTP 方法（接口权限用）',
  `sort_no`     int          NOT NULL DEFAULT 0,
  `status`      tinyint(1)   NOT NULL DEFAULT 1,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`  tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_perm_code` (`perm_code`),
  KEY `idx_parent` (`parent_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='权限（菜单/按钮/接口）';

DROP TABLE IF EXISTS `sys_user_role`;
CREATE TABLE `sys_user_role` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id`     bigint unsigned NOT NULL,
  `role_id`     bigint unsigned NOT NULL,
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_role` (`user_id`,`role_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户-角色';

DROP TABLE IF EXISTS `sys_role_permission`;
CREATE TABLE `sys_role_permission` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT,
  `role_id`       bigint unsigned NOT NULL,
  `permission_id` bigint unsigned NOT NULL,
  `create_time`   datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_role_perm` (`role_id`,`permission_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色-权限';

DROP TABLE IF EXISTS `sys_user_warehouse`;
CREATE TABLE `sys_user_warehouse` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id`     bigint unsigned NOT NULL,
  `wh_id`       bigint unsigned NOT NULL COMMENT '可访问仓库ID（base_location 中 loc_type=1）',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_wh` (`user_id`,`wh_id`),
  KEY `idx_wh` (`wh_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户数据权限（仓库范围）';

DROP TABLE IF EXISTS `sys_operation_log`;
CREATE TABLE `sys_operation_log` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id`       bigint unsigned NOT NULL,
  `user_name`     varchar(64)   NOT NULL COMMENT '操作人姓名快照',
  `op_time`       datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `ip`            varchar(64)   DEFAULT NULL,
  `module`        varchar(64)   DEFAULT NULL COMMENT '业务模块',
  `op_type`       varchar(20)   NOT NULL COMMENT 'CREATE/UPDATE/DELETE/AUDIT/CONFIRM/CANCEL/EXPORT/LOGIN/LOGOUT/ADJUST',
  `biz_id`        bigint unsigned DEFAULT NULL,
  `biz_no`        varchar(64)   DEFAULT NULL,
  `params_digest` varchar(1000) DEFAULT NULL COMMENT '脱敏后的请求参数摘要',
  `result`        varchar(10)   NOT NULL DEFAULT 'SUCCESS' COMMENT 'SUCCESS/FAIL',
  `error_msg`     varchar(200)  DEFAULT NULL,
  `cost_ms`       int           DEFAULT NULL,
  `user_agent`    varchar(255)  DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_op_time` (`op_time`),
  KEY `idx_user_time` (`user_id`,`op_time`),
  KEY `idx_biz` (`biz_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作审计日志（仅追加）';

DROP TABLE IF EXISTS `sys_login_log`;
CREATE TABLE `sys_login_log` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id`     bigint unsigned DEFAULT NULL,
  `username`    varchar(64) NOT NULL,
  `login_time`  datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `ip`          varchar(64) DEFAULT NULL,
  `result`      varchar(10) NOT NULL COMMENT 'SUCCESS/FAIL',
  `fail_reason` varchar(128) DEFAULT NULL,
  `user_agent`  varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_login_time` (`login_time`),
  KEY `idx_username_time` (`username`,`login_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='登录日志';

DROP TABLE IF EXISTS `sys_config`;
CREATE TABLE `sys_config` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT,
  `config_key`   varchar(64)  NOT NULL,
  `config_value` varchar(255) NOT NULL,
  `remark`       varchar(255) DEFAULT NULL,
  `update_by`    bigint unsigned DEFAULT NULL,
  `update_time`  datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_config_key` (`config_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统参数';

DROP TABLE IF EXISTS `sys_dict`;
CREATE TABLE `sys_dict` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `dict_type`   varchar(64)  NOT NULL COMMENT '字典类型，如 unit / adjust_reason',
  `dict_name`   varchar(64)  NOT NULL,
  `remark`      varchar(255) DEFAULT NULL,
  `is_deleted`  tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dict_type` (`dict_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据字典';

DROP TABLE IF EXISTS `sys_dict_item`;
CREATE TABLE `sys_dict_item` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `dict_type`   varchar(64)  NOT NULL,
  `item_value`  varchar(64)  NOT NULL,
  `item_label`  varchar(64)  NOT NULL,
  `sort_no`     int          NOT NULL DEFAULT 0,
  `status`      tinyint(1)   NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_type_value` (`dict_type`,`item_value`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='字典明细';

DROP TABLE IF EXISTS `sys_seq`;
CREATE TABLE `sys_seq` (
  `seq_key`     varchar(64) NOT NULL COMMENT '如 RK:20260901',
  `seq_value`   bigint      NOT NULL DEFAULT 0,
  `update_time` datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`seq_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='单号序列（Redis 不可用时的回退）';

-- -----------------------------------------------------------------------------
-- 2. 基础资料
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `base_goods_category`;
CREATE TABLE `base_goods_category` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT,
  `category_code` varchar(64)  NOT NULL,
  `category_name` varchar(64)  NOT NULL,
  `parent_id`     bigint unsigned NOT NULL DEFAULT 0,
  `sort_no`       int          NOT NULL DEFAULT 0,
  `status`        tinyint(1)   NOT NULL DEFAULT 1,
  `create_by`     bigint unsigned DEFAULT NULL,
  `create_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`     bigint unsigned DEFAULT NULL,
  `update_time`   datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`    tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_category_code` (`category_code`),
  KEY `idx_parent` (`parent_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品分类';

DROP TABLE IF EXISTS `base_spu`;
CREATE TABLE `base_spu` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `spu_code`    varchar(64)  NOT NULL COMMENT '款号（SPU）',
  `spu_name`    varchar(128) NOT NULL COMMENT '款名（如：中性笔 0.5mm）',
  `category_id` bigint unsigned NOT NULL DEFAULT 0,
  `brand`       varchar(64)  DEFAULT NULL COMMENT '品牌',
  `unit`        varchar(32)  DEFAULT NULL COMMENT '销售单位（快照）',
  `description` varchar(500) DEFAULT NULL COMMENT '卖点/描述（商城前台展示）',
  `status`      tinyint(1)   NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  `create_by`   bigint unsigned DEFAULT NULL,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`   bigint unsigned DEFAULT NULL,
  `update_time` datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`  tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_spu_code` (`spu_code`),
  KEY `idx_spu_category` (`category_id`),
  KEY `idx_spu_name` (`spu_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品款（SPU）：一个款可对应多个可卖 SKU';

-- 商品档案在 V4 里就是「SKU 表」：库存、订单行、上下架价、流水都挂在 base_goods.id 上
DROP TABLE IF EXISTS `base_goods`;
CREATE TABLE `base_goods` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT COMMENT 'SKU ID',
  `goods_code`   varchar(64)  NOT NULL COMMENT 'SKU 编码（唯一，不可修改）',
  `goods_name`   varchar(128) NOT NULL COMMENT 'SKU 名称（如：中性笔 0.5mm 红色）',
  `spu_id`       bigint unsigned NOT NULL DEFAULT 0 COMMENT '所属款 ID（base_spu.id，0=未归属）',
  `category_id`  bigint unsigned NOT NULL COMMENT '分类ID',
  `spec`         varchar(128) DEFAULT NULL COMMENT '规格型号（展示用）',
  `spec_attrs`   json         DEFAULT NULL COMMENT '规格属性，如 {"颜色":"红色","尺码":"M"}；商城按此渲染规格选择器',
  `unit`         varchar(32)  NOT NULL COMMENT '计量单位（字典 unit）',
  `barcode`      varchar(64)  DEFAULT NULL COMMENT '条码',
  `cost_price`   decimal(18,4) NOT NULL DEFAULT 0 COMMENT '成本价（入库计价参考）',
  `avg_cost`     decimal(18,4) NOT NULL DEFAULT 0 COMMENT '移动加权平均成本（入库更新、出库结转、毛利依据）',
  `warning_low`  decimal(18,3) NOT NULL DEFAULT 0 COMMENT '最低库存预警（补货建议亦可用 wms_stock_policy）',
  `warning_high` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '最高库存预警',
  `status`       tinyint(1)   NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  `remark`       varchar(255) DEFAULT NULL,
  `create_by`    bigint unsigned DEFAULT NULL,
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`    bigint unsigned DEFAULT NULL,
  `update_time`  datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`   tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_goods_code` (`goods_code`),
  KEY `idx_name` (`goods_name`),
  KEY `idx_barcode` (`barcode`),
  KEY `idx_category` (`category_id`),
  KEY `idx_spu` (`spu_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品档案（= SKU）：库存与交易挂载的最小单位';

DROP TABLE IF EXISTS `base_location`;
CREATE TABLE `base_location` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `loc_code`    varchar(64)  NOT NULL COMMENT '编码（现场标识一致）',
  `loc_name`    varchar(128) NOT NULL,
  `parent_id`   bigint unsigned NOT NULL DEFAULT 0 COMMENT '0=根',
  `loc_type`    tinyint      NOT NULL COMMENT '1仓库 2库区 3货架 4库位',
  `wh_id`       bigint unsigned NOT NULL DEFAULT 0 COMMENT '所属仓库ID（冗余，数据权限过滤用）',
  `sort_no`     int          NOT NULL DEFAULT 0,
  `status`      tinyint(1)   NOT NULL DEFAULT 1,
  `create_by`   bigint unsigned DEFAULT NULL,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`   bigint unsigned DEFAULT NULL,
  `update_time` datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`  tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_loc_code` (`loc_code`),
  KEY `idx_parent` (`parent_id`),
  KEY `idx_wh_type` (`wh_id`,`loc_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='仓库/库区/货架/库位';

DROP TABLE IF EXISTS `base_supplier`;
CREATE TABLE `base_supplier` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT,
  `supplier_code` varchar(64)  NOT NULL,
  `supplier_name` varchar(128) NOT NULL,
  `contact`       varchar(64)  DEFAULT NULL,
  `phone`         varchar(20)  DEFAULT NULL,
  `address`       varchar(255) DEFAULT NULL,
  `status`        tinyint(1)   NOT NULL DEFAULT 1,
  `remark`        varchar(255) DEFAULT NULL,
  `create_by`     bigint unsigned DEFAULT NULL,
  `create_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`     bigint unsigned DEFAULT NULL,
  `update_time`   datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`    tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_supplier_code` (`supplier_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='供应商';

DROP TABLE IF EXISTS `base_customer`;
CREATE TABLE `base_customer` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT,
  `customer_code` varchar(64)  NOT NULL,
  `customer_name` varchar(128) NOT NULL,
  `contact`       varchar(64)  DEFAULT NULL,
  `phone`         varchar(20)  DEFAULT NULL,
  `address`       varchar(255) DEFAULT NULL,
  `status`        tinyint(1)   NOT NULL DEFAULT 1,
  `remark`        varchar(255) DEFAULT NULL,
  `create_by`     bigint unsigned DEFAULT NULL,
  `create_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`     bigint unsigned DEFAULT NULL,
  `update_time`   datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`    tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_customer_code` (`customer_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户';

DROP TABLE IF EXISTS `base_shop`;
CREATE TABLE `base_shop` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT,
  `shop_code`    varchar(64)  NOT NULL COMMENT '店铺编码',
  `shop_name`    varchar(128) NOT NULL COMMENT '店铺名称',
  `channel_type` varchar(32)  NOT NULL DEFAULT 'self' COMMENT 'self自营商城 / taobao淘宝 / douyin抖音 / pdd拼多多 / offline线下',
  `app_key`      varchar(128) DEFAULT NULL COMMENT '平台 AppKey（密文/密钥不落库，仅存引用名）',
  `status`       tinyint(1)   NOT NULL DEFAULT 1 COMMENT '1启用 0停用（停用即整店停止接单）',
  `remark`       varchar(255) DEFAULT NULL,
  `create_by`    bigint unsigned DEFAULT NULL,
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`    bigint unsigned DEFAULT NULL,
  `update_time`  datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`   tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_shop_code` (`shop_code`),
  KEY `idx_channel` (`channel_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='渠道/店铺（自营商城 + 平台店铺，预留平台对接）';

-- -----------------------------------------------------------------------------
-- 3. 库存与流水（并发核心）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `wms_inventory`;
CREATE TABLE `wms_inventory` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '库存ID',
  `goods_id`      bigint unsigned NOT NULL,
  `loc_id`        bigint unsigned NOT NULL,
  `batch_no`      varchar(64)  NOT NULL DEFAULT '' COMMENT '批次号（无批次管理存空串，避免NULL参与唯一索引）',
  `quantity`      decimal(18,3) NOT NULL DEFAULT 0 COMMENT '实物数量',
  `locked_qty`    decimal(18,3) NOT NULL DEFAULT 0 COMMENT '预占数量（已审核未过账）',
  `available_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '可用量=quantity-locked_qty',
  `version`       int unsigned NOT NULL DEFAULT 0 COMMENT '乐观锁版本号',
  `expire_date`   date         DEFAULT NULL COMMENT '到期日（二期）',
  `in_time`       datetime     DEFAULT NULL COMMENT '最近入库时间（FIFO依据）',
  `update_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_goods_loc_batch` (`goods_id`,`loc_id`,`batch_no`),
  KEY `idx_loc_goods` (`loc_id`,`goods_id`),
  KEY `idx_goods_available` (`goods_id`,`available_qty`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实时库存';

DROP TABLE IF EXISTS `wms_inventory_txn`;
CREATE TABLE `wms_inventory_txn` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `txn_no`      varchar(64)  NOT NULL COMMENT '流水号',
  `biz_type`    tinyint      NOT NULL COMMENT '1采购入 2退货入 3其他入 4销售出 5领料出 6调拨出 7调拨入 8盘盈 9盘亏 10手工调整 11期初',
  `biz_id`      bigint unsigned NOT NULL COMMENT '来源单据ID',
  `biz_no`      varchar(64)  DEFAULT NULL,
  `detail_id`   bigint unsigned NOT NULL DEFAULT 0 COMMENT '来源明细ID',
  `goods_id`    bigint unsigned NOT NULL,
  `loc_id`      bigint unsigned NOT NULL,
  `batch_no`    varchar(64)  NOT NULL DEFAULT '',
  `change_qty`  decimal(18,3) NOT NULL COMMENT '变动数量（正入负出）',
  `before_qty`  decimal(18,3) NOT NULL COMMENT '变动前数量',
  `after_qty`   decimal(18,3) NOT NULL COMMENT '变动后数量',
  `unit_cost`   decimal(18,4) NOT NULL DEFAULT 0 COMMENT '本笔成本单价（入库=计价，出库=当时均价）',
  `cost_amount` decimal(18,4) NOT NULL DEFAULT 0 COMMENT '本笔成本金额（出库成本结转/毛利依据）',
  `create_by`   bigint unsigned NOT NULL,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_txn_no` (`txn_no`),
  UNIQUE KEY `uk_biz_detail` (`biz_type`,`biz_id`,`detail_id`),
  KEY `idx_goods_time` (`goods_id`,`create_time`),
  KEY `idx_loc_time` (`loc_id`,`create_time`),
  KEY `idx_biz_no` (`biz_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存流水（台账）';

-- -----------------------------------------------------------------------------
-- 4. 入库
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `wms_in_order`;
CREATE TABLE `wms_in_order` (
  `id`               bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_no`         varchar(64)  NOT NULL COMMENT '入库单号',
  `order_type`       tinyint      NOT NULL COMMENT '1采购入库 2退货入库 3其他入库',
  `supplier_id`      bigint unsigned DEFAULT NULL,
  `wh_id`            bigint unsigned NOT NULL,
  `status`           tinyint      NOT NULL DEFAULT 0 COMMENT '0草稿 1已审核 2部分入库 3已入库 4已作废',
  `total_plan_num`   decimal(18,3) NOT NULL DEFAULT 0,
  `total_actual_num` decimal(18,3) NOT NULL DEFAULT 0,
  `request_id`       varchar(64)  DEFAULT NULL COMMENT '过账幂等键',
  `audit_by`         bigint unsigned DEFAULT NULL,
  `audit_time`       datetime     DEFAULT NULL,
  `cancel_reason`    varchar(255) DEFAULT NULL,
  `remark`           varchar(255) DEFAULT NULL,
  `create_by`        bigint unsigned NOT NULL,
  `create_time`      datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`        bigint unsigned DEFAULT NULL,
  `update_time`      datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`       tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  UNIQUE KEY `uk_request_id` (`request_id`),
  KEY `idx_status_time` (`status`,`create_time`),
  KEY `idx_wh_time` (`wh_id`,`create_time`),
  KEY `idx_supplier` (`supplier_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='入库单主表';

DROP TABLE IF EXISTS `wms_in_order_detail`;
CREATE TABLE `wms_in_order_detail` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id`     bigint unsigned NOT NULL,
  `goods_id`     bigint unsigned NOT NULL,
  `loc_id`       bigint unsigned NOT NULL COMMENT '入库库位',
  `plan_num`     decimal(18,3) NOT NULL DEFAULT 0 COMMENT '计划数',
  `actual_num`   decimal(18,3) NOT NULL DEFAULT 0 COMMENT '实收数',
  `batch_no`     varchar(64)  NOT NULL DEFAULT '',
  `produce_date` date         DEFAULT NULL,
  `expire_date`  date         DEFAULT NULL,
  `unit`         varchar(32)  DEFAULT NULL COMMENT '单位快照',
  `remark`       varchar(255) DEFAULT NULL,
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`  datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`   tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_order` (`order_id`),
  KEY `idx_goods` (`goods_id`),
  KEY `idx_loc` (`loc_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='入库单明细';

-- -----------------------------------------------------------------------------
-- 5. 出库
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `wms_out_order`;
CREATE TABLE `wms_out_order` (
  `id`               bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_no`         varchar(64)  NOT NULL COMMENT '出库单号',
  `order_type`       tinyint      NOT NULL COMMENT '1销售出库 2领料出库 3调拨出库',
  `customer_id`      bigint unsigned DEFAULT NULL,
  `wh_id`            bigint unsigned NOT NULL,
  `status`           tinyint      NOT NULL DEFAULT 0 COMMENT '0草稿 1已审核 2拣货中 3已出库 4已作废',
  `total_plan_num`   decimal(18,3) NOT NULL DEFAULT 0,
  `total_actual_num` decimal(18,3) NOT NULL DEFAULT 0,
  `request_id`       varchar(64)  DEFAULT NULL COMMENT '过账幂等键',
  `audit_by`         bigint unsigned DEFAULT NULL,
  `audit_time`       datetime     DEFAULT NULL,
  `cancel_reason`    varchar(255) DEFAULT NULL,
  `remark`           varchar(255) DEFAULT NULL,
  `create_by`        bigint unsigned NOT NULL,
  `create_time`      datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`        bigint unsigned DEFAULT NULL,
  `update_time`      datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`       tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  UNIQUE KEY `uk_request_id` (`request_id`),
  KEY `idx_status_time` (`status`,`create_time`),
  KEY `idx_wh_time` (`wh_id`,`create_time`),
  KEY `idx_customer` (`customer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='出库单主表';

DROP TABLE IF EXISTS `wms_out_order_detail`;
CREATE TABLE `wms_out_order_detail` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id`      bigint unsigned NOT NULL,
  `goods_id`      bigint unsigned NOT NULL,
  `loc_id`        bigint unsigned NOT NULL COMMENT '出库库位（FIFO 推荐或人工指定）',
  `plan_num`      decimal(18,3) NOT NULL DEFAULT 0,
  `actual_num`    decimal(18,3) NOT NULL DEFAULT 0,
  `batch_no`      varchar(64)  NOT NULL DEFAULT '',
  `inv_version`   int unsigned DEFAULT NULL COMMENT '过账时的库存版本号（并发排查用）',
  `unit`          varchar(32)  DEFAULT NULL,
  `remark`        varchar(255) DEFAULT NULL,
  `create_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`   datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`    tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_order` (`order_id`),
  KEY `idx_goods` (`goods_id`),
  KEY `idx_loc` (`loc_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='出库单明细';

-- -----------------------------------------------------------------------------
-- 6. 盘点（二期，结构先建）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `wms_stocktake_order`;
CREATE TABLE `wms_stocktake_order` (
  `id`             bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_no`       varchar(64) NOT NULL,
  `stocktake_type` tinyint     NOT NULL DEFAULT 1 COMMENT '1全盘 2抽盘',
  `wh_id`          bigint unsigned NOT NULL,
  `status`         tinyint     NOT NULL DEFAULT 0 COMMENT '0草稿 1盘点中 2待审核 3已完成 4已作废',
  `plan_count`     int         NOT NULL DEFAULT 0,
  `actual_count`   int         NOT NULL DEFAULT 0,
  `diff_count`     int         NOT NULL DEFAULT 0,
  `diff_in_qty`    decimal(18,3) NOT NULL DEFAULT 0 COMMENT '盘盈合计',
  `diff_out_qty`   decimal(18,3) NOT NULL DEFAULT 0 COMMENT '盘亏合计',
  `audit_by`       bigint unsigned DEFAULT NULL,
  `audit_time`     datetime    DEFAULT NULL,
  `remark`         varchar(255) DEFAULT NULL,
  `create_by`      bigint unsigned NOT NULL,
  `create_time`    datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`      bigint unsigned DEFAULT NULL,
  `update_time`    datetime    DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`     tinyint(1)  NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  KEY `idx_status_time` (`status`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='盘点单主表';

DROP TABLE IF EXISTS `wms_stocktake_detail`;
CREATE TABLE `wms_stocktake_detail` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id`    bigint unsigned NOT NULL,
  `goods_id`    bigint unsigned NOT NULL,
  `loc_id`      bigint unsigned NOT NULL,
  `batch_no`    varchar(64)  NOT NULL DEFAULT '',
  `book_qty`    decimal(18,3) NOT NULL DEFAULT 0 COMMENT '账面数（生成时快照）',
  `actual_qty`  decimal(18,3) DEFAULT NULL COMMENT '实盘数',
  `diff_qty`    decimal(18,3) NOT NULL DEFAULT 0 COMMENT '盈亏（盘盈正、盘亏负）',
  `diff_reason` varchar(255) DEFAULT NULL,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`  tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_order` (`order_id`),
  KEY `idx_goods` (`goods_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='盘点单明细';

-- -----------------------------------------------------------------------------
-- 7. 调拨
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `wms_transfer_order`;
CREATE TABLE `wms_transfer_order` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_no`     varchar(64) NOT NULL,
  `from_wh_id`   bigint unsigned NOT NULL,
  `to_wh_id`     bigint unsigned NOT NULL,
  `status`       tinyint     NOT NULL DEFAULT 0 COMMENT '0草稿 1已审核 2已调拨 3已作废',
  `total_num`    decimal(18,3) NOT NULL DEFAULT 0,
  `request_id`   varchar(64) DEFAULT NULL,
  `audit_by`     bigint unsigned DEFAULT NULL,
  `audit_time`    datetime    DEFAULT NULL,
  `cancel_reason` varchar(255) DEFAULT NULL COMMENT '作废原因',
  `remark`        varchar(255) DEFAULT NULL,
  `create_by`     bigint unsigned NOT NULL,
  `create_time`   datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`     bigint unsigned DEFAULT NULL,
  `update_time`   datetime    DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`    tinyint(1)  NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  UNIQUE KEY `uk_request_id` (`request_id`),
  KEY `idx_status_time` (`status`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='调拨单主表（仓间/库位间，审核预占→确认两条流水）';

DROP TABLE IF EXISTS `wms_transfer_detail`;
CREATE TABLE `wms_transfer_detail` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id`    bigint unsigned NOT NULL,
  `goods_id`    bigint unsigned NOT NULL,
  `from_loc_id` bigint unsigned NOT NULL,
  `to_loc_id`   bigint unsigned NOT NULL,
  `batch_no`    varchar(64)  NOT NULL DEFAULT '',
  `num`         decimal(18,3) NOT NULL DEFAULT 0,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`  tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_order` (`order_id`),
  KEY `idx_goods` (`goods_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='调拨单明细';

-- =============================================================================
-- 8. 电商域（V4 · 订单驱动）：渠道上下架 / 订单 / 分配 / 波次 / 拣货 / 包裹 / 售后 / 补货政策
--
-- 设计要点：
--   · 订单（ec_order）是「用户购买」的唯一承载物；下单不动库存，只有「支付」与「发货」两处动物理库存。
--   · ec_order_alloc 是「订单行 → 库位×批次」的桥：支持拆单、多货位、FIFO 分配、批次追溯。
--   · 发货/退货入库用独立 biz_type（21/22），见文件头约定 7。
-- =============================================================================

DROP TABLE IF EXISTS `ec_listing`;
CREATE TABLE `ec_listing` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `shop_id`     bigint unsigned NOT NULL COMMENT '店铺ID（base_shop.id）',
  `goods_id`    bigint unsigned NOT NULL COMMENT 'SKU ID（base_goods.id）',
  `sale_price`  decimal(18,4) NOT NULL DEFAULT 0 COMMENT '渠道售价（下单时快照到订单行）',
  `sale_status` tinyint(1)    NOT NULL DEFAULT 1 COMMENT '1上架 0下架（下架即刻停止接单）',
  `reserve_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '渠道保留量（不参与本渠道售卖，给线下/其他渠道留货）',
  `sort_no`     int           NOT NULL DEFAULT 0,
  `create_by`   bigint unsigned DEFAULT NULL,
  `create_time` datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`   bigint unsigned DEFAULT NULL,
  `update_time` datetime      DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`  tinyint(1)    NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_shop_goods` (`shop_id`,`goods_id`),
  KEY `idx_listing_status` (`shop_id`,`sale_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品上下架（渠道×SKU）';

DROP TABLE IF EXISTS `ec_order`;
CREATE TABLE `ec_order` (
  `id`               bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_no`         varchar(64)  NOT NULL COMMENT '内部订单号 DS+yyyyMMdd+4位',
  `channel_order_no` varchar(64)  DEFAULT NULL COMMENT '平台订单号（渠道对账用）',
  `shop_id`          bigint unsigned NOT NULL,
  `order_kind`       tinyint      NOT NULL DEFAULT 1 COMMENT '1销售单 2换货单 3补发单',
  `parent_order_id`  bigint unsigned DEFAULT NULL COMMENT '换货/补发对应的原订单ID',
  `buyer_name`       varchar(64)  NOT NULL,
  `buyer_phone`      varchar(20)  DEFAULT NULL,
  `receiver_name`    varchar(64)  DEFAULT NULL,
  `receiver_phone`   varchar(20)  DEFAULT NULL,
  `receiver_address` varchar(255) DEFAULT NULL,
  `status`           tinyint      NOT NULL DEFAULT 0 COMMENT '0待付款 1待发货(已预占) 2拣货中 3已发货 4已完成 5已取消 6售后中 7待打包',
  `goods_amount`     decimal(18,4) NOT NULL DEFAULT 0 COMMENT '商品金额',
  `freight`          decimal(18,4) NOT NULL DEFAULT 0 COMMENT '运费',
  `pay_amount`       decimal(18,4) NOT NULL DEFAULT 0 COMMENT '实付金额',
  `request_id`       varchar(64)  DEFAULT NULL COMMENT '支付幂等键',
  `cancel_reason`    varchar(255) DEFAULT NULL,
  `pay_time`         datetime     DEFAULT NULL,
  `ship_time`        datetime     DEFAULT NULL,
  `finish_time`      datetime     DEFAULT NULL COMMENT '签收/完成时间（售后窗口起算点）',
  `create_by`        bigint unsigned DEFAULT NULL,
  `create_time`      datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`        bigint unsigned DEFAULT NULL,
  `update_time`      datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`       tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ec_order_no` (`order_no`),
  UNIQUE KEY `uk_ec_request_id` (`request_id`),
  KEY `idx_ec_status_time` (`status`,`create_time`),
  KEY `idx_ec_shop_time` (`shop_id`,`create_time`),
  KEY `idx_ec_parent` (`parent_order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商城订单（用户购买的唯一承载物）';

DROP TABLE IF EXISTS `ec_order_item`;
CREATE TABLE `ec_order_item` (
  `id`         bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id`   bigint unsigned NOT NULL,
  `goods_id`   bigint unsigned NOT NULL COMMENT 'SKU ID',
  `goods_code` varchar(64)   DEFAULT NULL COMMENT '下单时 SKU 编码快照',
  `goods_name` varchar(128)  DEFAULT NULL COMMENT '下单时商品名快照',
  `price`      decimal(18,4) NOT NULL DEFAULT 0 COMMENT '成交单价（快照，不随改价变化）',
  `qty`        decimal(18,3) NOT NULL DEFAULT 0,
  `amount`     decimal(18,4) NOT NULL DEFAULT 0,
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ec_item_order` (`order_id`),
  KEY `idx_ec_item_goods` (`goods_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单明细（价格与名称均为快照）';

DROP TABLE IF EXISTS `ec_order_alloc`;
CREATE TABLE `ec_order_alloc` (
  `id`         bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id`   bigint unsigned NOT NULL,
  `item_id`    bigint unsigned NOT NULL COMMENT 'ec_order_item.id',
  `goods_id`   bigint unsigned NOT NULL,
  `loc_id`     bigint unsigned NOT NULL COMMENT '分配到的末级库位',
  `batch_no`   varchar(64)  NOT NULL DEFAULT '',
  `alloc_qty`  decimal(18,3) NOT NULL DEFAULT 0 COMMENT '分配数量（已计入 locked_qty）',
  `status`     tinyint      NOT NULL DEFAULT 1 COMMENT '1已分配 2已拣货 3已发货 4已释放',
  `create_time` datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime    DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_alloc_order` (`order_id`),
  KEY `idx_alloc_status` (`status`,`goods_id`),
  KEY `idx_alloc_loc` (`loc_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存分配：订单行 → 库位×批次（拆单/多货位/批次追溯的基础）';

DROP TABLE IF EXISTS `ec_wave`;
CREATE TABLE `ec_wave` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `wave_no`     varchar(64) NOT NULL COMMENT '波次号 BC+yyyyMMdd+4位',
  `shop_id`     bigint unsigned DEFAULT NULL,
  `wh_id`       bigint unsigned DEFAULT NULL COMMENT '一个波次只允许同仓',
  `status`      tinyint     NOT NULL DEFAULT 0 COMMENT '0待拣 1拣货中 2待打包 3已完成 4已取消',
  `order_count` int         NOT NULL DEFAULT 0,
  `item_count`  int         NOT NULL DEFAULT 0,
  `total_qty`   decimal(18,3) NOT NULL DEFAULT 0,
  `finish_time` datetime    DEFAULT NULL,
  `create_by`   bigint unsigned DEFAULT NULL,
  `create_time` datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`   bigint unsigned DEFAULT NULL,
  `update_time` datetime    DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_wave_no` (`wave_no`),
  KEY `idx_wave_status` (`status`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='波次（电商仓的作业单位：一批订单一次拣货）';

DROP TABLE IF EXISTS `ec_wave_order`;
CREATE TABLE `ec_wave_order` (
  `id`       bigint unsigned NOT NULL AUTO_INCREMENT,
  `wave_id`  bigint unsigned NOT NULL,
  `order_id` bigint unsigned NOT NULL,
  `status`   tinyint NOT NULL DEFAULT 0 COMMENT '0待拣 1已拣货 2待打包 3已发货',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_wave_order` (`wave_id`,`order_id`),
  KEY `idx_wo_order` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='波次-订单关系';

DROP TABLE IF EXISTS `ec_pick_task`;
CREATE TABLE `ec_pick_task` (
  `id`         bigint unsigned NOT NULL AUTO_INCREMENT,
  `wave_id`    bigint unsigned NOT NULL,
  `order_id`   bigint unsigned NOT NULL,
  `alloc_id`   bigint unsigned NOT NULL COMMENT 'ec_order_alloc.id（一条分配一条任务）',
  `goods_id`   bigint unsigned NOT NULL,
  `loc_id`     bigint unsigned NOT NULL,
  `batch_no`   varchar(64)  NOT NULL DEFAULT '',
  `qty`        decimal(18,3) NOT NULL DEFAULT 0 COMMENT '应拣数',
  `picked_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '实拣数（少于应拣会被复核拦下）',
  `status`     tinyint      NOT NULL DEFAULT 0 COMMENT '0待拣 1已拣货 2缺货',
  `create_time` datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime    DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_pick_alloc` (`alloc_id`),
  KEY `idx_pick_wave` (`wave_id`,`status`),
  KEY `idx_pick_loc` (`loc_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='拣货任务（一行=一个库位的取货动作，按 loc 排序即拣货路径）';

DROP TABLE IF EXISTS `ec_package`;
CREATE TABLE `ec_package` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `package_no`  varchar(64) NOT NULL COMMENT '包裹号 BZ+yyyyMMdd+4位',
  `order_id`    bigint unsigned NOT NULL,
  `weight_g`    int         NOT NULL DEFAULT 0 COMMENT '包裹重量（克）',
  `remark`      varchar(255) DEFAULT NULL,
  `create_by`   bigint unsigned DEFAULT NULL,
  `create_time` datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_package_no` (`package_no`),
  KEY `idx_package_order` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='包裹（打包称重的产物，发货交接凭据）';

DROP TABLE IF EXISTS `ec_after_sale`;
CREATE TABLE `ec_after_sale` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT,
  `after_no`     varchar(64)  NOT NULL COMMENT '售后单号 SH+yyyyMMdd+4位',
  `order_id`     bigint unsigned NOT NULL,
  `item_id`      bigint unsigned DEFAULT NULL,
  `goods_id`     bigint unsigned NOT NULL,
  `qty`          decimal(18,3) NOT NULL DEFAULT 0,
  `type`         tinyint      NOT NULL DEFAULT 1 COMMENT '1退货退款 2仅退款 3换货 4补发',
  `status`       tinyint      NOT NULL DEFAULT 0 COMMENT '0待审核 1同意待收货 2已完成 3已拒绝',
  `reason`       varchar(255) DEFAULT NULL,
  `loc_id`       bigint unsigned DEFAULT NULL COMMENT '退回库位（默认原发货库位）',
  `batch_no`     varchar(64)  NOT NULL DEFAULT '',
  `good_qty`     decimal(18,3) NOT NULL DEFAULT 0 COMMENT '质检良品数（回原库位可再售）',
  `bad_qty`      decimal(18,3) NOT NULL DEFAULT 0 COMMENT '质检残次数（进次品区，不可售；良品+残次=退货数）',
  `check_time`   datetime     DEFAULT NULL,
  `check_by`     bigint unsigned DEFAULT NULL,
  `ref_order_no` varchar(64)  DEFAULT NULL COMMENT '换货/补发自动生成的替换订单号',
  `finish_time`  datetime     DEFAULT NULL,
  `create_by`    bigint unsigned DEFAULT NULL,
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_by`    bigint unsigned DEFAULT NULL,
  `update_time`  datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `is_deleted`   tinyint(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_after_no` (`after_no`),
  KEY `idx_after_order` (`order_id`),
  KEY `idx_after_status` (`status`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='售后（退货退款/仅退款/换货/补发 + 退货质检结果）';

DROP TABLE IF EXISTS `wms_stock_policy`;
CREATE TABLE `wms_stock_policy` (
  `id`            bigint unsigned NOT NULL AUTO_INCREMENT,
  `goods_id`      bigint unsigned NOT NULL,
  `wh_id`         bigint unsigned NOT NULL DEFAULT 0 COMMENT '0=该 SKU 全仓汇总口径',
  `safety_stock`  decimal(18,3) NOT NULL DEFAULT 0 COMMENT '安全库存（可用量低于它即建议补货）',
  `replenish_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '补货批量（0=按缺口补）',
  `lead_days`     int          NOT NULL DEFAULT 7 COMMENT '供应商交期（天）',
  `update_by`     bigint unsigned DEFAULT NULL,
  `update_time`   datetime     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_policy_goods_wh` (`goods_id`,`wh_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='补货政策（安全库存水位线）';

SET FOREIGN_KEY_CHECKS = 1;

-- =============================================================================
-- 初始化数据（密码哈希请由应用首次启动时生成并打印，不要写死在脚本里）
-- =============================================================================
INSERT INTO `sys_role` (`role_code`,`role_name`,`data_scope`,`remark`) VALUES
  ('admin',            '系统管理员', 1, '全部权限（含系统管理与运维）'),
  ('ec_operator',      '电商运营',   1, '订单池/上下架/补货/售后审核；不做仓储作业'),
  ('warehouse_manager','仓库主管',   3, '本仓库全部业务权限+审核+报表'),
  ('warehouse_keeper', '仓管员',     3, '收发货录入与确认、拣货复核'),
  ('picker',           '拣货员',     3, '波次拣货与打包，无取消/审核权'),
  ('packer',           '打包员',     3, '复核后打包称重与发货交接'),
  ('stocktaker',       '盘点员',     3, '盘点录入与提交，无审核权'),
  ('viewer',           '报表查看',   2, '只读查询与导出');

-- V4：自营商城店铺（平台店铺由接入时新增，channel_type 区分来源）
INSERT INTO `base_shop` (`shop_code`,`shop_name`,`channel_type`,`remark`) VALUES
  ('DS001','官方商城','self','自营商城（演示默认店铺）');

INSERT INTO `sys_config` (`config_key`,`config_value`,`remark`) VALUES
  ('order.no.rule',            'PREFIX+yyyyMMdd+SEQ4', '单据编号规则'),
  ('inventory.fifo.enabled',   'true',   '出库默认按 FIFO 推荐批次'),
  ('inventory.negative.allowed','false', '是否允许负库存（默认不允许）'),
  ('inventory.fefo.enabled',   'false',  '出库是否按 FEFO（近效期先出）；false=纯 FIFO'),
  ('inventory.lock.expire.hours','24',   '预占超时提醒阈值（小时）'),
  ('idempotent.cache.minutes', '10',     '幂等结果缓存时长（分钟）'),
  ('session.timeout.minutes',  '30',     '前端无操作自动登出（分钟）'),
  ('login.fail.lock.count',    '5',      '连续登录失败锁定次数'),
  ('password.expire.days',     '90',     '密码有效期（天）'),
  ('export.sync.max.rows',     '20000',  '同步导出上限行数'),
  ('upload.max.rows',          '5000',   'Excel 导入单次行数上限'),
  ('report.retain.months',     '36',     '报表数据在线保留月数');

INSERT INTO `sys_dict` (`dict_type`,`dict_name`,`remark`) VALUES
  ('unit','计量单位','含小数单位的商品按 3 位小数存储'),
  ('biz_type','库存变动类型','与 wms_inventory_txn.biz_type 对应'),
  ('adjust_reason','库存调整原因','库存数量调整必填'),
  ('in_order_type','入库类型','1采购 2退货 3其他'),
  ('out_order_type','出库类型','1销售 2领料 3调拨');

INSERT INTO `sys_dict_item` (`dict_type`,`item_value`,`item_label`,`sort_no`) VALUES
  ('unit','个','个',1),('unit','件','件',2),('unit','箱','箱',3),('unit','包','包',4),
  ('unit','千克','千克',5),('unit','米','米',6),('unit','卷','卷',7),
  ('biz_type','1','采购入库',1),('biz_type','2','退货入库',2),('biz_type','3','其他入库',3),
  ('biz_type','4','销售出库',4),('biz_type','5','领料出库',5),('biz_type','6','调拨出库',6),
  ('biz_type','7','调拨入库',7),('biz_type','8','盘盈',8),('biz_type','9','盘亏',9),
  ('biz_type','10','手工调整',10),('biz_type','11','期初',11),
  ('adjust_reason','盘盈','盘盈',1),('adjust_reason','盘亏','盘亏',2),
  ('adjust_reason','损耗','损耗',3),('adjust_reason','录错','录错',4),
  ('adjust_reason','漏记','漏记',5),('adjust_reason','其他','其他',6),
  ('in_order_type','1','采购入库',1),('in_order_type','2','退货入库',2),('in_order_type','3','其他入库',3),
  ('out_order_type','1','销售出库',1),('out_order_type','2','领料出库',2),('out_order_type','3','调拨出库',3),
  -- V4 电商：发货/退货入库用独立 biz_type，绝不能复用 1/2/4（见文件头约定 7）
  ('biz_type','21','电商发货出库',21),('biz_type','22','电商退货入库',22);

-- V4 电商常量（订单/波次/售后状态与类型，前后端与报表共用同一套码位）
INSERT INTO `sys_dict` (`dict_type`,`dict_name`,`remark`) VALUES
  ('ec_order_status','订单状态','0待付款 1待发货 2拣货中 3已发货 4已完成 5已取消 6售后中 7待打包'),
  ('ec_after_sale_type','售后类型','1退货退款 2仅退款 3换货 4补发')
ON DUPLICATE KEY UPDATE `dict_name`=VALUES(`dict_name`);

INSERT INTO `sys_dict_item` (`dict_type`,`item_value`,`item_label`,`sort_no`) VALUES
  ('ec_order_status','0','待付款',0),('ec_order_status','1','待发货',1),('ec_order_status','2','拣货中',2),
  ('ec_order_status','7','待打包',3),('ec_order_status','3','已发货',4),('ec_order_status','4','已完成',5),
  ('ec_order_status','5','已取消',6),('ec_order_status','6','售后中',7),
  ('ec_after_sale_type','1','退货退款',1),('ec_after_sale_type','2','仅退款',2),
  ('ec_after_sale_type','3','换货',3),('ec_after_sale_type','4','补发',4);

-- V4 电商参数（运维层可改：ec.* / inventory.* 走 sys_config 白名单）
INSERT INTO `sys_config` (`config_key`,`config_value`,`remark`) VALUES
  ('ec.free.shipping.amount',  '99',   '满多少免运费（元）'),
  ('ec.freight.fee',           '8',    '运费（未满免邮门槛，元）'),
  ('ec.auto.cancel.hours',     '24',   '未支付订单超时自动关闭（小时）'),
  ('ec.channel.self.enabled',  'true', '自营商城渠道开关（停用即整站停止接单）')
ON DUPLICATE KEY UPDATE `config_value`=VALUES(`config_value`);

DROP TABLE IF EXISTS `sys_job_log`;
CREATE TABLE `sys_job_log` (
  `id`       bigint unsigned NOT NULL AUTO_INCREMENT,
  `job_code` varchar(64) NOT NULL COMMENT '任务编码：AUTO_CANCEL_UNPAID / STALE_ALLOC_SCAN / EXPIRY_SCAN',
  `run_at`   datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `affected` int         NOT NULL DEFAULT 0,
  `detail`   varchar(255) DEFAULT NULL,
  `result`   varchar(10) NOT NULL DEFAULT 'SUCCESS',
  PRIMARY KEY (`id`),
  KEY `idx_job_time` (`run_at`),
  KEY `idx_job_code` (`job_code`,`run_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='批处理任务日志（超时关单/预占扫描/临期扫描）';

-- =============================================================================
-- 每日一致性校验 SQL（由定时任务执行，差异写入校验报告并告警）
-- =============================================================================
-- ① 可用量恒等式
-- SELECT COUNT(*) AS bad_rows FROM wms_inventory WHERE available_qty <> quantity - locked_qty;
-- ② 库存非负
-- SELECT COUNT(*) AS bad_rows FROM wms_inventory WHERE quantity < 0 OR locked_qty < 0 OR available_qty < 0;
-- ③ 流水可累加（应按 goods_id+loc_id+batch_no 与库存一致）
-- SELECT t.goods_id, t.loc_id, t.batch_no, SUM(t.change_qty) AS flow_qty, i.quantity
--   FROM wms_inventory_txn t
--   JOIN wms_inventory i ON i.goods_id=t.goods_id AND i.loc_id=t.loc_id AND i.batch_no=t.batch_no
--  GROUP BY t.goods_id, t.loc_id, t.batch_no, i.quantity
-- HAVING flow_qty <> i.quantity;
-- ④ 已过账单据与流水条数一致性
-- SELECT o.id, o.order_no, COUNT(d.id) AS detail_cnt,
--        (SELECT COUNT(*) FROM wms_inventory_txn t WHERE t.biz_id=o.id AND t.biz_type=1) AS txn_cnt
--   FROM wms_in_order o JOIN wms_in_order_detail d ON d.order_id=o.id
--  WHERE o.status=3 AND o.is_deleted=0 GROUP BY o.id, o.order_no HAVING detail_cnt <> txn_cnt;
-- ⑤ 长期未过账单据（预占长期占用）
-- SELECT id, order_no, audit_time FROM wms_out_order
--  WHERE status IN (1,2) AND audit_time < DATE_SUB(NOW(), INTERVAL 24 HOUR) AND is_deleted=0;
-- ⑥ 预占总额可解释（V4）：locked 必须能由「未过账出库单 + 未发货电商分配」完全解释
-- SELECT ABS((SELECT IFNULL(SUM(locked_qty),0) FROM wms_inventory)
--   - ((SELECT IFNULL(SUM(d.plan_num),0) FROM wms_out_order o JOIN wms_out_order_detail d ON d.order_id=o.id
--        WHERE o.status IN (1,2) AND o.is_deleted=0)
--    + (SELECT IFNULL(SUM(alloc_qty),0) FROM ec_order_alloc WHERE status IN (1,2)))) AS diff;
-- ⑦ 电商已发货订单必有流水（V4）：每一条发货分配都要有对应的 biz_type=21 流水
-- SELECT o.id, o.order_no,
--        (SELECT COUNT(*) FROM ec_order_alloc a WHERE a.order_id=o.id AND a.status=3) AS alloc_cnt,
--        (SELECT COUNT(*) FROM wms_inventory_txn t WHERE t.biz_type=21 AND t.biz_id=o.id) AS txn_cnt
--   FROM ec_order o WHERE o.status IN (3,4,6) AND o.is_deleted=0
-- HAVING alloc_cnt <> txn_cnt;
-- ⑧ 退货质检守恒（V4）：良品+残次必须等于退货数量，完成的售后单必须已质检
-- SELECT id, after_no, qty, good_qty, bad_qty FROM ec_after_sale
--  WHERE status=2 AND type IN (1,3) AND (good_qty + bad_qty <> qty OR check_time IS NULL);
-- ⑨ 拣货短拣检查（V4）：复核通过（待打包）的订单不允许存在实拣<应拣的任务
-- SELECT t.id, t.wave_id, t.qty, t.picked_qty FROM ec_pick_task t
--   JOIN ec_wave_order wo ON wo.order_id=t.order_id
--  WHERE wo.status >= 2 AND (t.status <> 1 OR t.picked_qty < t.qty);

-- =============================================================================
-- V4 增量升级（存量库执行；全新安装无需执行，上面的 CREATE TABLE 已含这些列）
--   MySQL 8 不支持 ADD COLUMN IF NOT EXISTS，执行前请先用
--   SHOW COLUMNS FROM base_goods LIKE 'spu_id'; 确认列不存在，避免重复执行报错。
-- =============================================================================
-- ALTER TABLE `base_goods`
--   ADD COLUMN `spu_id`     bigint unsigned NOT NULL DEFAULT 0 COMMENT '所属款ID' AFTER `goods_name`,
--   ADD COLUMN `spec_attrs` json DEFAULT NULL COMMENT '规格属性 JSON' AFTER `spec`,
--   ADD KEY `idx_spu` (`spu_id`);
-- ALTER TABLE `ec_order`
--   ADD COLUMN `order_kind`      tinyint NOT NULL DEFAULT 1 COMMENT '1销售单 2换货单 3补发单' AFTER `shop_id`,
--   ADD COLUMN `parent_order_id` bigint unsigned DEFAULT NULL COMMENT '原订单ID' AFTER `order_kind`;
-- ALTER TABLE `ec_after_sale`
--   ADD COLUMN `good_qty`     decimal(18,3) NOT NULL DEFAULT 0 COMMENT '良品数' AFTER `batch_no`,
--   ADD COLUMN `bad_qty`      decimal(18,3) NOT NULL DEFAULT 0 COMMENT '残次数' AFTER `good_qty`,
--   ADD COLUMN `check_time`   datetime DEFAULT NULL COMMENT '质检时间' AFTER `bad_qty`,
--   ADD COLUMN `check_by`     bigint unsigned DEFAULT NULL COMMENT '质检人' AFTER `check_time`,
--   ADD COLUMN `ref_order_no` varchar(64) DEFAULT NULL COMMENT '换货/补发替换单号' AFTER `check_by`;
-- V5（成本 / 效期 / 盘点调拨补列）
-- ALTER TABLE `base_goods`
--   ADD COLUMN `avg_cost` decimal(18,4) NOT NULL DEFAULT 0 COMMENT '移动加权平均成本' AFTER `cost_price`;
-- ALTER TABLE `wms_inventory_txn`
--   ADD COLUMN `unit_cost`   decimal(18,4) NOT NULL DEFAULT 0 COMMENT '成本单价' AFTER `after_qty`,
--   ADD COLUMN `cost_amount` decimal(18,4) NOT NULL DEFAULT 0 COMMENT '成本金额' AFTER `unit_cost`;
-- ALTER TABLE `wms_stocktake_order`
--   ADD COLUMN `diff_in_qty`  decimal(18,3) NOT NULL DEFAULT 0 COMMENT '盘盈合计' AFTER `diff_count`,
--   ADD COLUMN `diff_out_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '盘亏合计' AFTER `diff_in_qty`;
-- ALTER TABLE `wms_transfer_order`
--   ADD COLUMN `cancel_reason` varchar(255) DEFAULT NULL COMMENT '作废原因' AFTER `audit_time`;
-- 存量库初始化均价（只跑一次）：
-- UPDATE `base_goods` SET `avg_cost`=`cost_price` WHERE `avg_cost`=0;

-- =============================================================================
-- V8 生产化增量（幂等 / 任务队列 / 履约单 / 发货与拆单字段）
-- 说明：这三张表是「不重复扣库存」「峰值削峰」「跨仓拆单」的落库基础，
--       存量库按下面 ALTER 升级，新库可直接执行本段（放在主表之后）。
-- =============================================================================
CREATE TABLE IF NOT EXISTS `sys_idempotency` (
  `idem_key`    varchar(128) NOT NULL COMMENT '客户端幂等键（X-Idempotency-Key）',
  `method`      varchar(10)  NOT NULL,
  `path`        varchar(255) NOT NULL,
  `user_id`     bigint unsigned DEFAULT NULL,
  `state`       tinyint      NOT NULL DEFAULT 0 COMMENT '0处理中 1已完成',
  `status_code` int          DEFAULT NULL,
  `response`    mediumtext   DEFAULT NULL COMMENT '首次响应体（重放用）',
  `create_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `finish_time` datetime     DEFAULT NULL,
  PRIMARY KEY (`idem_key`),
  KEY `idx_idem_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='接口幂等（支付/发货/退款防重复执行）';

CREATE TABLE IF NOT EXISTS `sys_task_queue` (
  `id`           bigint unsigned NOT NULL AUTO_INCREMENT,
  `task_type`    varchar(64)  NOT NULL,
  `payload`      text         DEFAULT NULL,
  `status`       tinyint      NOT NULL DEFAULT 0 COMMENT '0待处理 1处理中 2成功 3失败',
  `attempts`     int          NOT NULL DEFAULT 0,
  `max_attempts` int          NOT NULL DEFAULT 5,
  `available_at` datetime     NOT NULL,
  `locked_by`    varchar(64)  DEFAULT NULL,
  `last_error`   varchar(500) DEFAULT NULL,
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_task_pick` (`status`,`available_at`),
  KEY `idx_task_type` (`task_type`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='任务队列（自动并波/清理/告警扫描，请求链路只入队）';

CREATE TABLE IF NOT EXISTS `ec_fulfillment` (
  `id`          bigint unsigned NOT NULL AUTO_INCREMENT,
  `order_id`    bigint unsigned NOT NULL,
  `wh_id`       bigint unsigned NOT NULL COMMENT '发货仓（拆单维度）',
  `seq`         int           NOT NULL DEFAULT 1,
  `status`      tinyint       NOT NULL DEFAULT 1 COMMENT '1待发货 2已发货',
  `total_qty`   decimal(18,3) NOT NULL DEFAULT 0,
  `shipped_qty` decimal(18,3) NOT NULL DEFAULT 0,
  `create_time` datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `ship_time`   datetime      DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ff_order_seq` (`order_id`,`seq`),
  KEY `idx_ff_order` (`order_id`,`status`),
  KEY `idx_ff_wh` (`wh_id`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='履约单：一个订单按仓拆成 1..N 张，各自发货';

-- 存量库升级（新库若已含上述列可跳过）
-- ALTER TABLE `ec_shipment`
--   ADD COLUMN `fulfillment_id` bigint unsigned DEFAULT NULL COMMENT '对应履约单' AFTER `status`,
--   ADD COLUMN `wh_id` bigint unsigned DEFAULT NULL COMMENT '发货仓' AFTER `fulfillment_id`,
--   ADD COLUMN `item_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '本单发货件数（<=0 视为空单异常）' AFTER `wh_id`,
--   ADD KEY `idx_ship_ff` (`fulfillment_id`), ADD KEY `idx_ship_wh` (`wh_id`,`create_time`);
-- ALTER TABLE `ec_order`
--   ADD COLUMN `ship_qty` decimal(18,3) NOT NULL DEFAULT 0 COMMENT '已发货件数（对账口径）' AFTER `pay_amount`,
--   ADD COLUMN `split_count` int NOT NULL DEFAULT 0 COMMENT '拆出的履约单数量' AFTER `ship_qty`,
--   ADD COLUMN `idem_key` varchar(128) DEFAULT NULL COMMENT '客户端幂等键' AFTER `request_id`;
-- ALTER TABLE `ec_order_alloc`
--   ADD COLUMN `fulfillment_id` bigint unsigned DEFAULT NULL COMMENT '归属履约单' AFTER `status`,
--   ADD COLUMN `wh_id` bigint unsigned DEFAULT NULL COMMENT '冗余仓库（按仓聚合/分片）' AFTER `fulfillment_id`,
--   ADD KEY `idx_alloc_ff` (`fulfillment_id`);

-- 分库分表建议（MySQL 8，按仓库水平拆分为主、时间为辅）：
--   1) 交易类（ec_order/ec_order_item/ec_order_alloc/ec_fulfillment/ec_shipment）
--      以 order_id 为唯一分片键（同一订单所有子表落同库），路由：order_id % N。
--   2) 库存类（wms_inventory/wms_inventory_txn/wms_stocktake*/wms_transfer*）
--      以 wh_id 为分片键（同仓事务不跨库），库内再按 create_time 做 RANGE 月分区。
--   3) 日志类（sys_operation_log/sys_login_log/sys_job_log/sys_task_queue）
--      按月分区，保留 6~12 个月，历史归档到冷库。
--   4) 跨分片查询只走汇总表（wms_daily_stat）与异步报表，禁止跨库 JOIN。

-- 发货单（原脚本遗漏：电商发货必须有单据，否则物流不可追溯、也无法做「已发货必有发货单」校验）
CREATE TABLE IF NOT EXISTS `ec_shipment` (
  `id`             bigint unsigned NOT NULL AUTO_INCREMENT,
  `shipment_no`    varchar(64) NOT NULL,
  `order_id`       bigint unsigned NOT NULL,
  `carrier`        varchar(32) DEFAULT NULL,
  `tracking_no`    varchar(64) DEFAULT NULL,
  `status`         tinyint NOT NULL DEFAULT 1 COMMENT '1已发货 2已签收',
  `fulfillment_id` bigint unsigned DEFAULT NULL COMMENT '对应履约单（按仓拆单）',
  `wh_id`          bigint unsigned DEFAULT NULL COMMENT '发货仓',
  `item_qty`       decimal(18,3) NOT NULL DEFAULT 0 COMMENT '本单发货件数（<=0 视为空单异常）',
  `create_time`    datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `create_by`      bigint unsigned DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_shipment_no` (`shipment_no`),
  KEY `idx_ship_order` (`order_id`),
  KEY `idx_ship_ff` (`fulfillment_id`),
  KEY `idx_ship_wh` (`wh_id`,`create_time`),
  KEY `idx_ship_tracking` (`tracking_no`),
  CONSTRAINT `fk_ship_order` FOREIGN KEY (`order_id`) REFERENCES `ec_order`(`id`),
  CONSTRAINT `fk_ship_ff` FOREIGN KEY (`fulfillment_id`) REFERENCES `ec_fulfillment`(`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='发货单：一张履约单对应一张发货单';

-- 日汇总 + 分布式限流（无 Redis 时用 MySQL 共享计数）
CREATE TABLE IF NOT EXISTS `wms_daily_stat` (
  `stat_date`    date          NOT NULL,
  `goods_id`     bigint unsigned NOT NULL,
  `wh_id`        bigint unsigned NOT NULL DEFAULT 0,
  `in_qty`       decimal(18,3) NOT NULL DEFAULT 0,
  `out_qty`      decimal(18,3) NOT NULL DEFAULT 0,
  `adjust_qty`   decimal(18,3) NOT NULL DEFAULT 0,
  `cost_amount`  decimal(18,4) NOT NULL DEFAULT 0,
  `sales_amount` decimal(18,4) NOT NULL DEFAULT 0,
  `end_qty`      decimal(18,3) NOT NULL DEFAULT 0,
  `update_time`  datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`stat_date`,`goods_id`,`wh_id`),
  KEY `idx_stat_goods` (`goods_id`,`stat_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日汇总：收发存/成本/销售额（报表只读这张表）';

CREATE TABLE IF NOT EXISTS `sys_rate_limit` (
  `bucket`      varchar(160) NOT NULL,
  `window_key`  varchar(20)  NOT NULL COMMENT 'YYYYMMDDHHMI',
  `hits`        int          NOT NULL DEFAULT 0,
  `update_time` datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`bucket`,`window_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='多实例令牌桶回退';

-- =============================================================================
-- 引用完整性（V8）：核心关系用真实外键兜底，防止孤儿数据
-- =============================================================================
ALTER TABLE `base_goods`      ADD CONSTRAINT `fk_goods_category` FOREIGN KEY (`category_id`) REFERENCES `base_goods_category`(`id`);
ALTER TABLE `base_goods`      ADD CONSTRAINT `fk_goods_spu`      FOREIGN KEY (`spu_id`)      REFERENCES `base_spu`(`id`);
ALTER TABLE `base_location`   ADD CONSTRAINT `fk_loc_parent`     FOREIGN KEY (`parent_id`)   REFERENCES `base_location`(`id`);
ALTER TABLE `wms_inventory`   ADD CONSTRAINT `fk_inv_goods`      FOREIGN KEY (`goods_id`)    REFERENCES `base_goods`(`id`);
ALTER TABLE `wms_inventory`   ADD CONSTRAINT `fk_inv_loc`        FOREIGN KEY (`loc_id`)      REFERENCES `base_location`(`id`);
ALTER TABLE `ec_order`        ADD CONSTRAINT `fk_order_shop`     FOREIGN KEY (`shop_id`)     REFERENCES `base_shop`(`id`);
ALTER TABLE `ec_order_item`   ADD CONSTRAINT `fk_item_order`     FOREIGN KEY (`order_id`)    REFERENCES `ec_order`(`id`);
ALTER TABLE `ec_order_alloc`  ADD CONSTRAINT `fk_alloc_order`    FOREIGN KEY (`order_id`)    REFERENCES `ec_order`(`id`);
ALTER TABLE `ec_fulfillment`  ADD CONSTRAINT `fk_ff_order`       FOREIGN KEY (`order_id`)   REFERENCES `ec_order`(`id`);
