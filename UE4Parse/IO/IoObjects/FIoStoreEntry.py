from typing import TYPE_CHECKING
from UE4Parse.Provider.Common import GameFile

if TYPE_CHECKING:
    from UE4Parse.IO.IoObjects.FIoChunkId import FIoChunkId
    from UE4Parse.IO.IoObjects.FIoOffsetAndLength import FIoOffsetAndLength


class FIoStoreEntry(GameFile):
    ioStore = None  # FFileIoStoreReader
    UserData: int
    Name: str
    Size: int = -1
    CompressionMethodIndex: int

    Encrypted: bool = False
    ChunkId: 'FIoChunkId'
    OffsetLength: 'FIoOffsetAndLength'

    def CompressionMethodString(self) -> str:
        return "COMPRESS_" + self.ioStore.TocResource.CompressionMethods[
            self.CompressionMethodIndex - 1] if self.CompressionMethodIndex > 0 else "COMPRESS_None"

    @property
    def Offset(self) -> int:
        return self.OffsetLength.GetOffset

    @property
    def Length(self) -> int:
        return self.OffsetLength.GetLength

    @property
    def ContainerName(self) -> str:
        return self.ioStore.FileName[:-5] + ".utoc"

    @property
    def Encrypted(self) -> bool:
        return self.ioStore.TocResource.Header.is_encrypted()

    @property
    def OffsetLength(self) -> 'FIoOffsetAndLength':
        return self.ioStore.Toc[self.ChunkId]

    @property
    def ChunkId(self) -> 'FIoChunkId':
        return self.ioStore.TocResource.ChunkIds[self._userdata]

    def __init__(self, io_store, userdata: int, name: str):
        super().__init__()
        self.ioStore = io_store
        self._userdata = userdata

        self.Name = name.lower() if io_store.caseinSensitive else name

        # compressionBlockSize = ioStore.TocResource.Header.CompressionBlockSize
        # firstBlockIndex = int(self.Offset / compressionBlockSize) - 1
        # lastBlockIndex = int((Align(self.Offset + self.Length, compressionBlockSize) - 1) / compressionBlockSize)

        # for i in range(firstBlockIndex, lastBlockIndex):
        #     compressionBlock = ioStore.TocResource.CompressionBlocks[i]
        #     self.UncompressedSize += compressionBlock.UncompressedSize
        #     self.CompressionMethodIndex = compressionBlock.CompressionMethodIndex
        #
        #     rawSize = Align(compressionBlock.CompressedSize, 16)
        #     self.Size += rawSize
        #
        #     if ioStore.TocResource.Header.is_encrypted():
        #         self.Encrypted = True

    def GetData(self):
        return self.ioStore.Read(self.ChunkId)
