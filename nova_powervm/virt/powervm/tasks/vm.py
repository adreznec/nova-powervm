# Copyright 2015, 2016 IBM Corp.
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_log import log as logging
from pypowervm.tasks import power
from pypowervm.tasks import storage as pvm_stg
import six
from taskflow.types import failure as task_fail

from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm.tasks import base as pvm_task
from nova_powervm.virt.powervm import vm

LOG = logging.getLogger(__name__)


class Get(pvm_task.PowerVMTask):

    """The task for getting a VM entry."""

    def __init__(self, adapter, host_uuid, instance):
        """Creates the Task for getting a VM entry.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID
        :param instance: The nova instance.
        """
        super(Get, self).__init__(instance, 'get_vm', provides='lpar_wrap')
        self.adapter = adapter
        self.host_uuid = host_uuid

    def execute_impl(self):
        return vm.get_instance_wrapper(self.adapter, self.instance)


class Create(pvm_task.PowerVMTask):

    """The task for creating a VM."""

    def __init__(self, adapter, host_wrapper, instance, flavor, stg_ftsk,
                 nvram_mgr=None):
        """Creates the Task for creating a VM.

        The revert method is not implemented because the compute manager
        calls the driver destroy operation for spawn errors.  By not deleting
        the lpar, it's a cleaner flow through the destroy operation and
        accomplishes the same result.

        Any stale storage associated with the new VM's (possibly recycled) ID
        will be cleaned up.  The cleanup work will be delegated to the FeedTask
        represented by the stg_ftsk parameter.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_wrapper: The managed system wrapper
        :param instance: The nova instance.
        :param flavor: The nova flavor.
        :param stg_ftsk: A FeedTask managing storage I/O operations.
        :param nvram_mgr: The NVRAM manager to fetch the NVRAM from. If None,
                          the NVRAM will not be fetched.
        """
        super(Create, self).__init__(
            instance, 'crt_vm', provides='lpar_wrap')
        self.adapter = adapter
        self.host_wrapper = host_wrapper
        self.flavor = flavor
        self.stg_ftsk = stg_ftsk
        self.nvram_mgr = nvram_mgr

    def execute_impl(self):
        data = None
        if self.nvram_mgr is not None:
            LOG.info(_LI('Fetching NVRAM for instance %s.'),
                     self.instance.name, instance=self.instance)
            data = self.nvram_mgr.fetch(self.instance)
            LOG.debug('NVRAM data is: %s', data, instance=self.instance)

        wrap = vm.crt_lpar(self.adapter, self.host_wrapper, self.instance,
                           self.flavor, nvram=data)
        pvm_stg.add_lpar_storage_scrub_tasks([wrap.id], self.stg_ftsk,
                                             lpars_exist=True)
        return wrap


class Resize(pvm_task.PowerVMTask):

    """The task for resizing an existing VM."""

    def __init__(self, adapter, host_wrapper, instance, flavor, name=None):
        """Creates the Task to resize a VM.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_wrapper: The managed system wrapper
        :param instance: The nova instance.
        :param flavor: The nova flavor.
        :param name: VM name to use for the update.  Used on resize when we
            want to rename it but not use the instance name.
        """
        super(Resize, self).__init__(
            instance, 'resize_vm', provides='lpar_wrap')
        self.adapter = adapter
        self.host_wrapper = host_wrapper
        self.flavor = flavor
        self.vm_name = name

    def execute_impl(self):
        return vm.update(self.adapter, self.host_wrapper,
                         self.instance, self.flavor, entry=None,
                         name=self.vm_name)


class Rename(pvm_task.PowerVMTask):

    """The task for renaming an existing VM."""

    def __init__(self, adapter, instance, name):
        """Creates the Task to rename a VM.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param instance: The nova instance.
        :param name: The new VM name.
        """
        super(Rename, self).__init__(
            instance, 'rename_vm_%s' % name, provides='lpar_wrap')
        self.adapter = adapter
        self.vm_name = name

    def execute_impl(self):
        LOG.info(_LI('Renaming instance to name: %s'), self.name,
                 instance=self.instance)
        return vm.rename(self.adapter, self.instance, self.vm_name)


class PowerOn(pvm_task.PowerVMTask):

    """The task to power on the instance."""

    def __init__(self, adapter, host_uuid, instance, pwr_opts=None,
                 synchronous=False):
        """Create the Task for the power on of the LPAR.

        Obtains LPAR info through requirement of lpar_wrap (provided by
        tf_crt_vm).

        :param adapter: The pypowervm adapter.
        :param host_uuid: The host UUID.
        :param instance: The nova instance.
        :param pwr_opts: Additional parameters for the pypowervm PowerOn Job.
        :param synchronous: (Optional) If False (the default), the Task
                            completes as soon as the pypowervm PowerOn Job has
                            successfully started.  If True, the Task waits for
                            the pypowervm PowerOn Job to complete.
        """
        super(PowerOn, self).__init__(
            instance, 'pwr_vm', requires=['lpar_wrap'])
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.pwr_opts = pwr_opts
        self.synchronous = synchronous

    def execute_impl(self, lpar_wrap):
        power.power_on(lpar_wrap, self.host_uuid, add_parms=self.pwr_opts,
                       synchronous=self.synchronous)

    def revert_impl(self, lpar_wrap, result, flow_failures):
        LOG.warning(_LW('Powering off instance: %s'), self.instance.name)

        if isinstance(result, task_fail.Failure):
            # The power on itself failed...can't power off.
            LOG.debug('Power on failed.  Not performing power off.')
            return

        power.power_off(lpar_wrap, self.host_uuid, force_immediate=True)


class PowerOff(pvm_task.PowerVMTask):

    """The task to power off a VM."""

    def __init__(self, adapter, host_uuid, lpar_uuid, instance,
                 force_immediate=False):
        """Creates the Task to power off an LPAR.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID
        :param lpar_uuid: The UUID of the lpar that has media.
        :param instance: The nova instance.
        :param force_immediate: Boolean. Perform a VSP hard power off.
        """
        super(PowerOff, self).__init__(instance, 'pwr_off_vm')
        self.adapter = adapter
        self.host_uuid = host_uuid
        self.lpar_uuid = lpar_uuid
        self.force_immediate = force_immediate

    def execute_impl(self):
        vm.power_off(self.adapter, self.instance, self.host_uuid,
                     force_immediate=self.force_immediate)


class StoreNvram(pvm_task.PowerVMTask):

    """Store the NVRAM for an instance."""

    def __init__(self, nvram_mgr, instance, immediate=False):
        """Creates a task to store the NVRAM of an instance.

        :param nvram_mgr: The NVRAM manager.
        :param instance: The nova instance.
        :param immediate: boolean whether to update the NVRAM immediately
        """
        super(StoreNvram, self).__init__(instance, 'store_nvram')
        self.nvram_mgr = nvram_mgr
        self.immediate = immediate

    def execute_impl(self):
        if self.nvram_mgr is None:
            return

        try:
            self.nvram_mgr.store(self.instance, immediate=self.immediate)
        except Exception as e:
            LOG.exception(_LE('Unable to store NVRAM for instance '
                              '%(name)s. Exception: %(reason)s'),
                          {'name': self.instance.name,
                           'reason': six.text_type(e)},
                          instance=self.instance)


class DeleteNvram(pvm_task.PowerVMTask):

    """Delete the NVRAM for an instance from the store."""

    def __init__(self, nvram_mgr, instance):
        """Creates a task to delete the NVRAM of an instance.

        :param nvram_mgr: The NVRAM manager.
        :param instance: The nova instance.
        """
        super(DeleteNvram, self).__init__(instance, 'delete_nvram')
        self.nvram_mgr = nvram_mgr

    def execute_impl(self):
        if self.nvram_mgr is None:
            LOG.info(_LI("No op for NVRAM delete."), instance=self.instance)
            return

        LOG.info(_LI('Deleting NVRAM for instance: %s'),
                 self.instance.name, instance=self.instance)
        try:
            self.nvram_mgr.remove(self.instance)
        except Exception as e:
            LOG.exception(_LE('Unable to delete NVRAM for instance '
                              '%(name)s. Exception: %(reason)s'),
                          {'name': self.instance.name,
                           'reason': six.text_type(e)},
                          instance=self.instance)


class Delete(pvm_task.PowerVMTask):

    """The task to delete the instance from the system."""

    def __init__(self, adapter, lpar_uuid, instance):
        """Create the Task to delete the VM from the system.

        :param adapter: The adapter for the pypowervm API.
        :param lpar_uuid: The VM's PowerVM UUID.
        :param instance: The nova instance.
        """
        super(Delete, self).__init__(instance, 'dlt_vm')
        self.adapter = adapter
        self.lpar_uuid = lpar_uuid

    def execute_impl(self):
        vm.dlt_lpar(self.adapter, self.lpar_uuid)


class UpdateIBMiSettings(pvm_task.PowerVMTask):

    """The task to update settings of an ibmi instance."""

    def __init__(self, adapter, instance, boot_type):
        """Create the Task to update settings of the IBMi VM.

        :param adapter: The adapter for the pypowervm API.
        :param instance: The nova instance.
        :param boot_type: The boot type of the instance.
        """
        super(UpdateIBMiSettings, self).__init__(
            instance, 'update_ibmi_settings')
        self.adapter = adapter
        self.boot_type = boot_type

    def execute_impl(self):
        vm.update_ibmi_settings(self.adapter, self.instance, self.boot_type)
