SET @add_wechat_homepage_image = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'wechat_homepage_image'
        ),
        'SELECT ''wechat_homepage_image exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN wechat_homepage_image VARCHAR(512) NULL COMMENT ''wechat homepage image path or url'' AFTER error_message'
    )
);
PREPARE stmt1 FROM @add_wechat_homepage_image;
EXECUTE stmt1;
DEALLOCATE PREPARE stmt1;

SET @add_wechat_moments_image = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'wechat_moments_image'
        ),
        'SELECT ''wechat_moments_image exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN wechat_moments_image VARCHAR(512) NULL COMMENT ''wechat moments image path or url'' AFTER wechat_homepage_image'
    )
);
PREPARE stmt2 FROM @add_wechat_moments_image;
EXECUTE stmt2;
DEALLOCATE PREPARE stmt2;
