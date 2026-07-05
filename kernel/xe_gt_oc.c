// SPDX-License-Identifier: MIT
/*
 * Copyright © 2026 Intel Corporation
 *
 * Intel Arc Pro (Battlemage) voltage-frequency curve overclocking.
 *
 * The GPU exposes an 85-point voltage-frequency (VF) curve through the PCODE
 * "late-binding" interface. Writing the curve is a bracketed transaction:
 *
 *   PCODE_MBOX(0x5f, 2, 0)            begin write session
 *   PCODE_MBOX(0x5d, 0xa, 3) d0=P    write point P = (mV << 8 | index), x85
 *   PCODE_MBOX(0x5d, 0xb, 3)         end / finalize
 *
 * Reading a point: PCODE_MBOX(0x5d, 8, 3) with DATA0 = index -> DATA0 = point.
 *
 * Without the begin command the point write is rejected by PCODE; that begin is
 * what the vendor driver issues and the stock driver omits, which is why
 * VF-curve tuning (overclock / undervolt) was unavailable on Linux.
 *
 * sysfs: <device>/tile#/gt#/oc/vf_curve  (read/write)
 *   read  -> one "<index> <voltage_mV>" line per point
 *   write -> one or more "<index> <voltage_mV>" lines; unlisted points keep
 *            their current value. Voltage is clamped to [OC_VMIN_MV, OC_VMAX_MV].
 *
 * sysfs: <device>/tile#/gt#/oc/mem_speed  (read/write)
 *   GDDR6 memory data rate in Mbps (e.g. 19000 = 19 Gbps). Set via PCODE
 *   LATE_BINDING domain 0x17: write (0x5e,6,0x17) d0=Mbps then commit
 *   (0x5e,8,0x17); read (0x5e,5,0x17). Clamped to [OC_MEM_MIN_MBPS,
 *   OC_MEM_MAX_MBPS]. Reads report the stock speed until a value is staged.
 *
 * sysfs: <device>/tile#/gt#/oc/temp_limit  (read/write)
 *   GPU thermal-throttle target in degrees C. Same LATE_BINDING mechanism,
 *   domain 0x49. Clamped to [OC_TEMP_MIN_C, OC_TEMP_MAX_C].
 */

#include <linux/cleanup.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>

#include <drm/drm_managed.h>

#include "xe_gt_oc.h"
#include "xe_gt.h"
#include "xe_gt_sysfs.h"
#include "xe_gt_types.h"
#include "xe_device.h"
#include "xe_pm.h"
#include "xe_tile.h"
#include "xe_pcode.h"
#include "xe_pcode_api.h"

#define OC_VF_NPTS	85
#define OC_VF_TABLE	3
#define OC_VMIN_MV	400
#define OC_VMAX_MV	1200

#define OC_MBOX_READ	PCODE_MBOX(0x5d, 0x8, OC_VF_TABLE)
#define OC_MBOX_WRITE	PCODE_MBOX(0x5d, 0xa, OC_VF_TABLE)
#define OC_MBOX_END	PCODE_MBOX(0x5d, 0xb, OC_VF_TABLE)
#define OC_MBOX_BEGIN	PCODE_MBOX(0x5f, 0x2, 0x0)

/* VRAM (GDDR6) memory speed, in Mbps, via LATE_BINDING domain 0x17:
 * read = (0x5e, 5, 0x17), set = (0x5e, 6, 0x17) d0=Mbps, commit = (0x5e, 8, 0x17). */
#define OC_MEM_DOMAIN		0x17
#define OC_MBOX_MEM_READ	PCODE_MBOX(0x5e, 0x5, OC_MEM_DOMAIN)
#define OC_MBOX_MEM_SET		PCODE_MBOX(0x5e, 0x6, OC_MEM_DOMAIN)
#define OC_MBOX_MEM_COMMIT	PCODE_MBOX(0x5e, 0x8, OC_MEM_DOMAIN)
#define OC_MEM_MIN_MBPS		14000
#define OC_MEM_MAX_MBPS		24000
#define OC_MEM_STOCK_MBPS	19000	/* B60/B70 GDDR6 default (2375 MHz x8); reported before anything is staged */

/* GPU temperature limit (thermal throttle target), in degrees C, via
 * LATE_BINDING domain 0x49: read (0x5e,5,0x49), set (0x5e,6,0x49) d0=degC,
 * commit (0x5e,8,0x49). */
#define OC_TEMP_DOMAIN		0x49
#define OC_MBOX_TEMP_READ	PCODE_MBOX(0x5e, 0x5, OC_TEMP_DOMAIN)
#define OC_MBOX_TEMP_SET	PCODE_MBOX(0x5e, 0x6, OC_TEMP_DOMAIN)
#define OC_MBOX_TEMP_COMMIT	PCODE_MBOX(0x5e, 0x8, OC_TEMP_DOMAIN)
#define OC_TEMP_MIN_C		60
#define OC_TEMP_MAX_C		100
#define OC_TEMP_STOCK_C		100	/* default throttle target */

static struct xe_device *oc_kobj_to_xe(struct kobject *kobj)
{
	return gt_to_xe(kobj_to_gt(kobj->parent));
}

static int oc_read_point(struct xe_tile *tile, u8 idx, u32 *packed)
{
	u32 val = idx, val1 = 0;
	int ret;

	ret = xe_pcode_read(tile, OC_MBOX_READ, &val, &val1);
	if (ret)
		return ret;

	*packed = val;
	return 0;
}

static ssize_t vf_curve_show(struct kobject *kobj, struct kobj_attribute *attr,
			     char *buf)
{
	struct xe_device *xe = oc_kobj_to_xe(kobj);
	struct xe_tile *tile = xe_device_get_root_tile(xe);
	ssize_t len = 0;
	u32 packed;
	int i, ret;

	guard(xe_pm_runtime)(xe);

	for (i = 0; i < OC_VF_NPTS; i++) {
		ret = oc_read_point(tile, i, &packed);
		if (ret)
			return ret;
		len += sysfs_emit_at(buf, len, "%d %u\n", i, packed >> 8);
	}

	return len;
}

static ssize_t vf_curve_store(struct kobject *kobj, struct kobj_attribute *attr,
			      const char *buf, size_t count)
{
	struct xe_device *xe = oc_kobj_to_xe(kobj);
	struct xe_tile *tile = xe_device_get_root_tile(xe);
	u32 mv[OC_VF_NPTS];
	u32 packed, end0 = 0, end1 = 0;
	const char *p = buf;
	int i, ret;

	guard(xe_pm_runtime)(xe);

	/* seed with the current curve so a partial write keeps unlisted points */
	for (i = 0; i < OC_VF_NPTS; i++) {
		ret = oc_read_point(tile, i, &packed);
		if (ret)
			return ret;
		mv[i] = packed >> 8;
	}

	while (p && *p) {
		unsigned int idx, volt;

		if (sscanf(p, "%u %u", &idx, &volt) == 2 && idx < OC_VF_NPTS)
			mv[idx] = clamp_t(unsigned int, volt,
					  OC_VMIN_MV, OC_VMAX_MV);
		p = strchr(p, '\n');
		if (p)
			p++;
	}

	/* transaction: begin -> write every point -> finalize */
	ret = xe_pcode_write64_timeout(tile, OC_MBOX_BEGIN, 0, 0, 1);
	if (ret)
		return ret;

	for (i = 0; i < OC_VF_NPTS; i++) {
		ret = xe_pcode_write64_timeout(tile, OC_MBOX_WRITE,
					       (mv[i] << 8) | i, 0, 1);
		if (ret)
			return ret;
	}

	xe_pcode_read(tile, OC_MBOX_END, &end0, &end1);

	return count;
}

static ssize_t mem_speed_show(struct kobject *kobj, struct kobj_attribute *attr,
			      char *buf)
{
	struct xe_device *xe = oc_kobj_to_xe(kobj);
	struct xe_tile *tile = xe_device_get_root_tile(xe);
	u32 mbps = 0, hi = 0;
	int ret;

	guard(xe_pm_runtime)(xe);

	/* p1=5 read returns -EPROTO until a value has been staged (fresh boot =
	 * hardware default); report the stock speed in that case. */
	ret = xe_pcode_read(tile, OC_MBOX_MEM_READ, &mbps, &hi);
	if (ret)
		mbps = OC_MEM_STOCK_MBPS;

	return sysfs_emit(buf, "%u\n", mbps);
}

static ssize_t mem_speed_store(struct kobject *kobj, struct kobj_attribute *attr,
			       const char *buf, size_t count)
{
	struct xe_device *xe = oc_kobj_to_xe(kobj);
	struct xe_tile *tile = xe_device_get_root_tile(xe);
	u32 mbps, c0 = 0, c1 = 0;
	int ret;

	ret = kstrtou32(buf, 0, &mbps);
	if (ret)
		return ret;
	if (mbps < OC_MEM_MIN_MBPS || mbps > OC_MEM_MAX_MBPS)
		return -EINVAL;

	guard(xe_pm_runtime)(xe);

	ret = xe_pcode_write64_timeout(tile, OC_MBOX_MEM_SET, mbps, 0, 1);
	if (ret)
		return ret;
	xe_pcode_read(tile, OC_MBOX_MEM_COMMIT, &c0, &c1);   /* commit / finalize */

	return count;
}

static ssize_t temp_limit_show(struct kobject *kobj, struct kobj_attribute *attr,
			       char *buf)
{
	struct xe_device *xe = oc_kobj_to_xe(kobj);
	struct xe_tile *tile = xe_device_get_root_tile(xe);
	u32 degc = 0, hi = 0;
	int ret;

	guard(xe_pm_runtime)(xe);

	ret = xe_pcode_read(tile, OC_MBOX_TEMP_READ, &degc, &hi);
	if (ret)
		degc = OC_TEMP_STOCK_C;

	return sysfs_emit(buf, "%u\n", degc);
}

static ssize_t temp_limit_store(struct kobject *kobj, struct kobj_attribute *attr,
				const char *buf, size_t count)
{
	struct xe_device *xe = oc_kobj_to_xe(kobj);
	struct xe_tile *tile = xe_device_get_root_tile(xe);
	u32 degc, c0 = 0, c1 = 0;
	int ret;

	ret = kstrtou32(buf, 0, &degc);
	if (ret)
		return ret;
	if (degc < OC_TEMP_MIN_C || degc > OC_TEMP_MAX_C)
		return -EINVAL;

	guard(xe_pm_runtime)(xe);

	ret = xe_pcode_write64_timeout(tile, OC_MBOX_TEMP_SET, degc, 0, 1);
	if (ret)
		return ret;
	xe_pcode_read(tile, OC_MBOX_TEMP_COMMIT, &c0, &c1);   /* commit / finalize */

	return count;
}

static struct kobj_attribute attr_vf_curve = __ATTR_RW(vf_curve);
static struct kobj_attribute attr_mem_speed = __ATTR_RW(mem_speed);
static struct kobj_attribute attr_temp_limit = __ATTR_RW(temp_limit);

static const struct attribute *oc_attrs[] = {
	&attr_vf_curve.attr,
	&attr_mem_speed.attr,
	&attr_temp_limit.attr,
	NULL,
};

static void oc_fini(void *arg)
{
	struct kobject *oc = arg;

	sysfs_remove_files(oc, oc_attrs);
	kobject_put(oc);
}

/**
 * xe_gt_oc_init - expose the overclocking sysfs interface for a GT
 * @gt: the GT
 *
 * Only the root tile's primary GT drives PCODE, so the "oc" directory is
 * created a single time, under that GT.
 */
int xe_gt_oc_init(struct xe_gt *gt)
{
	struct xe_device *xe = gt_to_xe(gt);
	struct kobject *oc;
	int err;

	if (xe->info.skip_pcode)
		return 0;

	if (gt != xe_device_get_root_tile(xe)->primary_gt)
		return 0;

	oc = kobject_create_and_add("oc", gt->sysfs);
	if (!oc)
		return -ENOMEM;

	err = sysfs_create_files(oc, oc_attrs);
	if (err) {
		kobject_put(oc);
		return err;
	}

	return devm_add_action_or_reset(xe->drm.dev, oc_fini, oc);
}
