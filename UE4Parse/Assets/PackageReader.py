from abc import ABC, abstractmethod
from enum import IntEnum
from functools import singledispatchmethod
import traceback

from UE4Parse.Assets.Exports.UObjects import UObject
from UE4Parse.IO.IoObjects import FIoStoreEntry
from UE4Parse.IoObjects.IoUtils import resolveObjectIndex
from typing import List, TYPE_CHECKING, Optional, Tuple, TypeVar, Union

from UE4Parse import Logger
from UE4Parse.Assets import ToJson
from UE4Parse.BinaryReader import BinaryStream
from UE4Parse.Exceptions.Exceptions import ParserException
from UE4Parse.IoObjects.FExportBundle import EExportCommandType, FExportBundle, FExportBundleEntry, FExportBundleHeader
from UE4Parse.IoObjects.FExportMapEntry import FExportMapEntry
from UE4Parse.IoObjects.FImportedPackage import FImportedPackage, FPackageId
from UE4Parse.IoObjects.FIoGlobalData import FIoGlobalData
from UE4Parse.IoObjects.FPackageObjectIndex import FPackageObjectIndex
from UE4Parse.IoObjects.FPackageSummary import FPackageSummary, FZenPackageSummary
from UE4Parse.Assets.Objects.FNameEntrySerialized import FNameEntrySerialized
from UE4Parse.Assets.Objects.FObjectExport import FObjectExport
from UE4Parse.Assets.Objects.FObjectImport import FObjectImport
from UE4Parse.Assets.Objects.FPackageFileSummary import FPackageFileSummary
from UE4Parse.Assets.Objects.FPackageIndex import FPackageIndex
from UE4Parse.Assets.Exports.ExportRegistry import Registry
from UE4Parse.Readers.FAssetReader import FAssetReader
from UE4Parse.Versions import EUEVersion

if TYPE_CHECKING:
    from UE4Parse.Provider import DefaultFileProvider
    from UE4Parse.IO.IoObjects.FIoContainerHeader import FIoContainerHeader

logger = Logger.get_logger(__name__)

T = TypeVar('T')


class EPackageLoadMode(IntEnum):
    """read package till..."""
    Full = 0
    NameMap = 1
    Info = 2


class Package(ABC):
    NameMap: List[FNameEntrySerialized]
    ImportMap: Tuple[Union[FObjectImport, FPackageObjectIndex]]
    ExportMap: Tuple[Union[FObjectExport, FExportMapEntry]]
    Summary: Union[FPackageFileSummary, FPackageSummary]
    Provider: 'DefaultFileProvider'

    def get_summary(self):
        return self.Summary

    def get_dict(self):
        return ToJson.ToJson(self)

    @abstractmethod
    def find_export(self, export_name: str) -> Optional[UObject]:
        pass

    @singledispatchmethod
    @abstractmethod
    def find_export_of_type(self, export_type: str) -> Optional[UObject]:
        pass

    @find_export_of_type.register
    def _(self, arg: UObject) -> Optional[T]:
        pass

    def find_export(self, export_name: str) -> Optional[UObject]:
        for export in self.ExportMap:
            if export_name == export.name.string:
                return export.exportObject
        return None


class LegacyPackageReader(Package):
    NameMap: List[FNameEntrySerialized] = []
    ImportMap: List[FObjectImport] = []
    ExportMap: List[FObjectExport] = []
    Summary: FPackageFileSummary

    # @profile
    def __init__(self, uasset: BinaryStream, uexp: BinaryStream = None, ubulk: BinaryStream = None,
                 provider: "DefaultFileProvider" = None, load_mode: EPackageLoadMode = EPackageLoadMode.Full,
                 version: EUEVersion = EUEVersion.LATEST) -> None:
        if provider:
            FAssetReader.provider = provider
            version = provider.Versions.UEVersion

        self.reader = FAssetReader(uasset.base_stream, self, size=uasset.size)
        self.reader.set_ar_version(version)
        self.reader.PackageReader = self

        if hasattr(uasset, 'mappings'):
            self.reader.mappings = uasset.mappings

        self.PackageFileSummary = FPackageFileSummary(self.reader)
        self.Summary = self.PackageFileSummary
        pos = self.reader.tell()
        self.NameMap = self.SerializeNameMap()
        if load_mode == EPackageLoadMode.NameMap: return

        self.ImportMap = self.SerializeImportMap()
        self.ExportMap = self.SerializeExportMap()

        if load_mode == EPackageLoadMode.Info: return

        filever = self.PackageFileSummary.FileVersionUE4
        if filever > 0:
            self.reader.version = filever

        if uexp is not None:
            self.reader = FAssetReader(uexp, self, uexp.size, uasset.size)
            self.reader.set_ar_version(version)
            self.reader.PackageReader = self

        self.reader.ubulk_stream = ubulk

        # what was this for?
        # elif self.PackageFileSummary.FileVersionUE4.value == 0:  # Cooked
        #     return
        # else:  # not cooked
        # self.reader.seek(pos, 0)
        # self.reader.change_stream(self.reader.read())

        for Export in self.ExportMap:
            if Export.ClassIndex.IsNull:
                ExportType = self.reader.readFName()
            elif Export.ClassIndex.IsExport:
                ExportType = self.ExportMap[Export.ClassIndex.AsExport].SuperIndex.Resource.ObjectName
            elif Export.ClassIndex.IsImport:
                ExportType = self.ImportMap[Export.ClassIndex.AsImport].ObjectName
            else:
                raise ParserException("failed to get export type")
            Export.type = ExportType

            self.reader.seek_absolute(Export.SerialOffset, 0)

            self.reader.bulk_offset = Export.SerialSize + self.PackageFileSummary.TotalHeaderSize  # ?

            pos = self.reader.base_stream.tell()
            ExportData = Registry().get_export_reader(ExportType.string, Export, self.reader)
            try:
                ExportData.deserialize(pos + Export.SerialSize)
            except Exception as e:
                logger.error(f"Could not read {ExportType.string} correctly, {e}")
            Export.exportObject = ExportData

            position = self.reader.base_stream.tell()
            if position != pos + Export.SerialSize:
                logger.debug(
                    f"Didn't read ExportType {ExportType.string} properly, at {position}, should be: {pos + Export.SerialSize} behind: {pos + Export.SerialSize - position}")

            # export event
            if provider:
                trigger = provider.Triggers.get(ExportType.string, False)
                if trigger:
                    trigger(ExportData)

    def SerializeNameMap(self):
        if self.PackageFileSummary.NameCount > 0:
            self.reader.seek(self.PackageFileSummary.NameOffset, 0)
            OutNameMap: List[FNameEntrySerialized] = []
            for _ in range(self.PackageFileSummary.NameCount):
                OutNameMap.append(FNameEntrySerialized(self.reader))
            return OutNameMap
        return []

    def SerializeImportMap(self):
        if self.PackageFileSummary.ImportCount > 0:
            self.reader.seek(self.PackageFileSummary.ImportOffset, 0)
            OutImportMap: List[FObjectImport] = []
            for _ in range(self.PackageFileSummary.ImportCount):
                OutImportMap.append(FObjectImport(self.reader))
            return OutImportMap
        return []

    def SerializeExportMap(self):
        if self.PackageFileSummary.ExportCount > 0:
            self.reader.seek(self.PackageFileSummary.ExportOffset, 0)
            OutExportMap: List[FObjectExport] = []
            for _ in range(self.PackageFileSummary.ExportCount):
                OutExportMap.append(FObjectExport(self.reader))
            return OutExportMap
        return []

    def get_summary(self) -> FPackageFileSummary:
        return self.PackageFileSummary

    @singledispatchmethod
    def find_export_of_type(self, export_type: str) -> Optional[UObject]:
        for export in self.ExportMap:
            if export_type == export.type.string:
                return export.exportObject
        return None

    @find_export_of_type.register
    def _(self, _cls: type) -> Optional[T]:
        for export in self.ExportMap:
            if isinstance(export.exportObject, _cls):
                return export.exportObject
        return None

    def findObject(self, index: FPackageIndex) -> Optional[Union[FObjectExport, FObjectImport]]:
        if index.IsNull:
            return None
        elif index.IsImport:
            return self.ImportMap[index.AsImport]
        else:
            export = self.ExportMap[index.AsExport]
            return export


class IoPackageReader(Package):
    Name: str
    GlobalData: FIoGlobalData
    Summary: Union[FPackageSummary, FZenPackageSummary]
    NameMap: Tuple[FNameEntrySerialized]
    ImportMap: Tuple[FPackageObjectIndex]
    ExportMap: Tuple[FExportMapEntry]
    ExportBundle: FExportBundle
    GraphData: Tuple[FImportedPackage]
    ImportedPackages: Tuple[Optional['IoPackageReader']]
    ImportedPublicExportHashes: Tuple[int]

    # noinspection PyTypeChecker
    def __init__(self, uasset: BinaryStream, ubulk: BinaryStream, uptnl: BinaryStream, provider: "DefaultFileProvider",
                 load_mode: EPackageLoadMode = EPackageLoadMode.Full, container_header: 'FIoContainerHeader' = None, package: 'FIoStoreEntry' = None):
        reader = FAssetReader(uasset.base_stream, self, size=uasset.size)
        reader.mappings = uasset.mappings
        reader.ubulk_stream = ubulk or uptnl
        reader.set_ar_version(provider.Versions.UEVersion)
        reader.PackageReader = self
        self.reader = reader
        self.Provider = provider
        self.NameMap = ()
        self.ImportMap = ()
        self.ExportMap = ()
        self.ImportedPublicExportHashes = ()

        if reader.game >= EUEVersion.GAME_UE5_0:
            self.Summary = FZenPackageSummary(reader)

            # noinspection PyUnresolvedReferences
            assert not self.Summary.bHasVersioningInfo
            temp_names = []
            self.NameMap = FNameEntrySerialized.LoadNameBatch2(temp_names, reader)
            self.NameMap = tuple(temp_names);
            del temp_names
            self.Name = self.Summary.Name.resolve(self.NameMap)
            if load_mode == EPackageLoadMode.NameMap: return

            store_entry = None
            if container_header is not None:
                try:
                    index = container_header.PackageIds.index(FPackageId.from_int(package.ChunkId.ChunkId))
                    store_entry = container_header.StoreEntries[index]
                except ValueError:
                    pass

            reader.seek(self.Summary.ImportedPublicExportHashesOffset, 0)
            self.ImportedPublicExportHashes = reader.readTArray2(reader.readUInt64, int((self.Summary.ImportMapOffset - self.Summary.ImportedPublicExportHashesOffset) / 8))

            reader.seek(self.Summary.ImportMapOffset, 0)
            import_map_Count = int(
                (self.Summary.ExportMapOffset - self.Summary.ImportMapOffset) / 8)  # size of FPackageObjectIndex
            self.ImportMap = tuple(FPackageObjectIndex(reader) for _ in range(import_map_Count))

            reader.seek(self.Summary.ExportMapOffset, 0)
            export_map_count = int(
                (self.Summary.ExportBundleEntriesOffset - self.Summary.ExportMapOffset) / FExportMapEntry.SIZE)
            self.ExportMap = tuple(FExportMapEntry(reader) for _ in range(export_map_count))

            reader.seek(self.Summary.ExportBundleEntriesOffset, 0)
            export_bundle_entries = reader.readTArray2(lambda: FExportBundleEntry(reader), export_map_count * 2)

            reader.seek(self.Summary.GraphDataOffset, 0)
            export_bundle_headers = reader.readTArray2(lambda: FExportBundleHeader(reader), store_entry.ExportBundleCount if store_entry else 1)

            self.ExportBundle = FExportBundle.from_data(export_bundle_headers, export_bundle_entries)

            if load_mode == EPackageLoadMode.Info: return

            imported = store_entry.ImportedPackages if store_entry else []
            self.ImportedPackages = tuple(provider.try_load_package(iD, EPackageLoadMode.Info) for iD in imported)
            del imported

            allExportDataOffset = self.Summary.HeaderSize
        else:
            self.Summary = FPackageSummary(reader=reader)

            if self.Summary.NameMapNamesSize > 0:
                reader.seek(self.Summary.NameMapNamesOffset, 0)
                nameMapReader = BinaryStream(reader.readBytes(self.Summary.NameMapNamesSize))

                reader.seek(self.Summary.NameMapHashesOffset, 0)
                nameHashReader = BinaryStream(reader.readBytes(self.Summary.NameMapHashesSize))
                hashCount = int(nameHashReader.size / 8 - 1)

                name_map = []
                FNameEntrySerialized.LoadNameBatch(name_map, nameMapReader, hashCount)
                self.NameMap = tuple(name_map)
                self.Name = self.Summary.Name.resolve(self.NameMap)
                del nameHashReader
                del nameMapReader
            if load_mode == EPackageLoadMode.NameMap: return

            reader.seek(self.Summary.ImportMapOffset, 0)
            import_map_Count = int(
                (self.Summary.ExportMapOffset - self.Summary.ImportMapOffset) / 8)  # size of FPackageObjectIndex
            self.ImportMap = tuple(FPackageObjectIndex(reader) for _ in range(import_map_Count))

            reader.seek(self.Summary.ExportMapOffset, 0)
            exportMapCount = int((self.Summary.ExportBundlesOffset - self.Summary.ExportMapOffset) / FExportMapEntry.SIZE)
            self.ExportMap = tuple(FExportMapEntry(reader) for _ in range(exportMapCount))

            reader.seek(self.Summary.ExportBundlesOffset, 0)
            self.ExportBundle = FExportBundle(reader, self.Summary.GraphDataOffset - self.Summary.ExportBundlesOffset)

            if load_mode == EPackageLoadMode.Info: return

            if provider.GlobalData is None:
                logger.error("Missing global data can't serialize")
                return

            reader.seek(self.Summary.GraphDataOffset, 0)
            self.GraphData = reader.readTArray(FImportedPackage, reader)
            self.ImportedPackages = tuple(
                provider.try_load_package(iD.index, EPackageLoadMode.Info) for iD in self.GraphData)

            allExportDataOffset = self.Summary.GraphDataOffset + self.Summary.GraphDataSize
        currentExportDataOffset = allExportDataOffset

        assert provider.GlobalData != None, "can't serialize without global data"

        for header in self.ExportBundle.Headers:
            for i in range(header.EntryCount):
                export_entry = self.ExportBundle.Entries[header.FirstEntryIndex + i]
                if export_entry.CommandType == EExportCommandType.ExportCommandType_Serialize:
                    Export = self.ExportMap[export_entry.LocalExportIndex]  # self.ExportMap[i]

                    self.reader.seek(currentExportDataOffset, 0)

                    export_resolved = resolveObjectIndex(self, provider.GlobalData, index=Export.ClassIndex)
                    if export_resolved is None: continue
                    export_type = export_resolved.getName()
                    ExportData = Registry().get_export_reader(export_type.string, Export, self.reader)
                    error = None
                    try:
                        ExportData.deserialize(currentExportDataOffset + Export.CookedSerialSize)
                    except Exception as e:
                        error = traceback.format_exc()

                    Export.type = export_type
                    Export.exportObject = ExportData

                    position = self.reader.base_stream.tell()
                    if position != currentExportDataOffset + Export.CookedSerialSize:
                        msg = f"Didn't read ExportType {export_type.string} properly, at {position}, should be: {currentExportDataOffset + Export.CookedSerialSize} behind: {currentExportDataOffset + Export.CookedSerialSize - position}"
                        if error is not None:
                            msg += f"\nError: {error}"
                        logger.debug(msg)

                    # export event
                    trigger = provider.Triggers.get(ExportData.type, False)
                    if trigger:
                        trigger(ExportData)

                    currentExportDataOffset += Export.CookedSerialSize

    @singledispatchmethod
    def find_export_of_type(self, export_type: str) -> Optional[UObject]:
        for export in self.ExportMap:
            if export_type == export.type.string:
                return export.exportObject
        return None

    @find_export_of_type.register
    def _(self, _cls: type) -> Optional[T]:
        for export in self.ExportMap:
            if isinstance(export.exportObject, _cls):
                return export.exportObject
        return None
