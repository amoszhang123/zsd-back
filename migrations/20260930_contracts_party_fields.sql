-- 合同表增加从 Excel 落款区解析的需方（甲方）开票与联系信息
-- 对应「打印采购单」合同 sheet 第 279~283 行需方栏（F 列标签 / G 列值）
--
--   address       地址（出货单打印用的收件地址）
--   bank_name     开户行
--   bank_account  账号
--   tax_no        税号
--   phone         电话

ALTER TABLE contracts
  ADD COLUMN address VARCHAR(300) NOT NULL DEFAULT '' AFTER party_a,
  ADD COLUMN bank_name VARCHAR(200) NOT NULL DEFAULT '' AFTER address,
  ADD COLUMN bank_account VARCHAR(60) NOT NULL DEFAULT '' AFTER bank_name,
  ADD COLUMN tax_no VARCHAR(60) NOT NULL DEFAULT '' AFTER bank_account,
  ADD COLUMN phone VARCHAR(40) NOT NULL DEFAULT '' AFTER tax_no;
